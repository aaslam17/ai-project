"""Security, Policy Enforcement, and Self-Evaluation Guardrail Plugin for ADK.

Implements a multi-layered `BasePlugin` protecting model inputs, high-stakes tool
invocations, and model outputs:
1. Input Guardrail (`before_model_callback`): Blocks prompt-injection / constitutional
   override attempts, scrubs PII in transient prompt logs, and optionally queries
   Google Cloud Model Armor (`google-cloud-modelarmor`).
2. Policy & HITL Tool Guardrail (`before_tool_callback`): Enforces maximum refund caps
   and requires prior `evaluate_refund_policy_eligibility` verification before refunding.
3. Output Self-Evaluation Guardrail (`after_model_callback`): Scrubs any accidental PII
   echoed by the LLM and blocks fabricated refund receipt IDs (`RFND-...`).
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any

from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.genai import types

from app.config import get_settings
from app.observability import emit_structured_event
from app.pii_redaction import get_pii_redactor

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.models.llm_request import LlmRequest
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext

_PROMPT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions"),
    re.compile(r"(?i)override\s+(?:system|agent|policy|security)\s+(?:constitution|rules|prompt)"),
    re.compile(r"(?i)you\s+are\s+now\s+(?:in\s+)?(?:developer|god|unrestricted|jailbreak)\s+mode"),
    re.compile(r"(?i)bypass\s+(?:human\s+approval|refund\s+policy|confirmation)"),
)

_RECEIPT_ID_PATTERN = re.compile(r"\bRFND-[A-Z0-9]{8}\b")


class SecurityAndPolicyGuardrailPlugin(BasePlugin):
    """Cross-cutting ADK guardrail plugin enforcing input safety, policy caps, and output self-eval."""

    def __init__(self, name: str = "security_and_policy_guardrail_plugin") -> None:
        super().__init__(name=name)
        self._model_armor_template = os.environ.get("MODEL_ARMOR_TEMPLATE_ID")

    def _extract_latest_user_text(self, llm_request: LlmRequest) -> str:
        contents = getattr(llm_request, "contents", None) or []
        for content in reversed(contents):
            if getattr(content, "role", None) == "user":
                parts = getattr(content, "parts", None) or []
                texts = [
                    p.text for p in parts if getattr(p, "text", None) is not None
                ]
                if texts:
                    return "\n".join(texts)
        return ""

    def _check_model_armor_if_configured(self, user_text: str) -> bool:
        """Optional Google Cloud Model Armor screening when template ID is configured."""
        if not self._model_armor_template or not user_text:
            return False
        try:
            from google.cloud import modelarmor_v1

            client = modelarmor_v1.ModelArmorClient()
            req = modelarmor_v1.SanitizeUserPromptRequest(
                name=self._model_armor_template,
                user_prompt_data=modelarmor_v1.DataItem(text=user_text),
            )
            resp = client.sanitize_user_prompt(request=req, timeout=3.0)
            match_state = getattr(
                resp.sanitization_result, "filter_match_state", None
            )
            return str(match_state).endswith("MATCH_FOUND")
        except Exception:
            return False

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        latest_user_text = self._extract_latest_user_text(llm_request)
        if not latest_user_text:
            return None

        matched_injection = any(
            pattern.search(latest_user_text) is not None
            for pattern in _PROMPT_INJECTION_PATTERNS
        )
        matched_armor = self._check_model_armor_if_configured(latest_user_text)

        if matched_injection or matched_armor:
            emit_structured_event(
                "GUARDRAIL_INPUT_BLOCKED",
                "Blocked adversarial prompt-injection / policy-bypass attempt.",
                level=logging.WARNING,
                phase="OUTCOME",
                guardrail="prompt_injection_shield",
                actual_outcome="blocked",
            )
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part.from_text(
                            text=(
                                "I cannot fulfill requests to ignore security rules or bypass "
                                "enterprise refund policies. Please share a valid order ID "
                                "(for example, 'ORD-1001') if you need help with an order."
                            )
                        )
                    ],
                )
            )
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        if tool.name != "issue_verified_customer_refund":
            return None

        settings = get_settings()
        raw_amount = tool_args.get("refund_amount_usd", 0.0)
        try:
            amount = float(raw_amount)
        except (TypeError, ValueError):
            amount = -1.0

        if amount <= 0 or amount > settings.max_auto_policy_refund_usd:
            emit_structured_event(
                "GUARDRAIL_TOOL_BLOCKED",
                f"Blocked out-of-bounds refund amount: {raw_amount}.",
                level=logging.WARNING,
                phase="OUTCOME",
                guardrail="refund_cap_policy",
                tool_name=tool.name,
                planned_args=tool_args,
            )
            return {
                "status": "error",
                "order_id": str(tool_args.get("order_id", "UNKNOWN")),
                "refunded_amount_usd": 0.0,
                "error_code": "GUARDRAIL_REFUND_LIMIT_EXCEEDED",
                "message": (
                    f"Refund amount ${amount:.2f} violates the enterprise guardrail cap "
                    f"of ${settings.max_auto_policy_refund_usd:.2f}."
                ),
                "recovery_instruction": (
                    "Do not retry `issue_verified_customer_refund` above the policy cap. "
                    "Use `create_priority_escalation_ticket` with priority 'P2' instead."
                ),
            }

        order_id = str(tool_args.get("order_id", "")).strip().upper()
        verified_orders = tool_context.state.get("verified_eligible_orders") or {}
        if order_id and order_id not in verified_orders:
            emit_structured_event(
                "GUARDRAIL_UNAUDITED_REFUND_BLOCKED",
                f"Blocked refund attempt for unaudited order '{order_id}'.",
                level=logging.WARNING,
                phase="OUTCOME",
                guardrail="mandatory_policy_audit_prerequisite",
                tool_name=tool.name,
            )
            return {
                "status": "error",
                "order_id": order_id,
                "refunded_amount_usd": 0.0,
                "error_code": "MISSING_POLICY_AUDIT",
                "message": (
                    f"Order '{order_id}' has not been verified via "
                    "`evaluate_refund_policy_eligibility` in this session."
                ),
                "recovery_instruction": (
                    f"Call `evaluate_refund_policy_eligibility` for '{order_id}' first, "
                    "confirm `eligible == True`, and only then invoke "
                    "`issue_verified_customer_refund`."
                ),
            }
        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        """Self-evaluates model text for leaked PII and hallucinated refund receipts."""
        content = getattr(llm_response, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            return None

        redactor = get_pii_redactor()
        issued_ids = set(callback_context.state.get("issued_refund_ids") or [])
        modified = False
        new_parts: list[types.Part] = []

        for part in parts:
            text_val = getattr(part, "text", None)
            if not text_val:
                new_parts.append(part)
                continue

            updated_text = text_val
            if redactor.contains_pii(updated_text):
                updated_text = redactor.redact_text(updated_text)
                modified = True
                emit_structured_event(
                    "GUARDRAIL_OUTPUT_PII_SCRUBBED",
                    "Self-eval guardrail scrubbed PII from model response.",
                    level=logging.WARNING,
                    phase="OUTCOME",
                    guardrail="output_pii_self_eval",
                )

            claimed_receipts = set(_RECEIPT_ID_PATTERN.findall(updated_text))
            unverified_receipts = claimed_receipts - issued_ids
            if unverified_receipts:
                for fake_id in unverified_receipts:
                    updated_text = updated_text.replace(
                        fake_id, "[UNVERIFIED_RECEIPT_BLOCKED]"
                    )
                modified = True
                emit_structured_event(
                    "GUARDRAIL_HALLUCINATED_RECEIPT_BLOCKED",
                    "Self-eval guardrail blocked unverified refund receipt ID in response.",
                    level=logging.WARNING,
                    phase="OUTCOME",
                    guardrail="hallucination_self_eval",
                )

            new_parts.append(
                types.Part.from_text(text=updated_text) if modified else part
            )

        if not modified:
            return None

        return LlmResponse(
            content=types.Content(
                role=getattr(content, "role", "model") or "model",
                parts=new_parts,
            )
        )
