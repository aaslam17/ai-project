"""Enterprise Support & Refund Resolution Tools with Schemas, Guided Errors & HITL Hooks.

Every tool provides:
1. Highly specific verb-noun naming (`lookup_customer_order_details`, etc.),
2. Comprehensive Google-style docstrings describing purpose, parameters, and returns,
3. Strict Pydantic v2 input and output JSON Schema validation (`app.schemas`),
4. Guided error handling returning actionable `recovery_instruction` dicts (never crashing),
5. Explicit Human-in-the-Loop confirmation hooks (`FunctionTool(..., require_confirmation=...)`)
   for high-stakes financial refunds and critical P1 escalations.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import FunctionTool, ToolContext
from pydantic import ValidationError

from app.config import get_settings
from app.pii_redaction import get_pii_redactor
from app.schemas import (
    EscalationTicketInput,
    EscalationTicketOutput,
    IssueRefundInput,
    OrderDetailsOutput,
    OrderLookupInput,
    RefundEligibilityInput,
    RefundEligibilityOutput,
    RefundReceiptOutput,
    format_validation_recovery,
)
from app.store import get_order_store


def lookup_customer_order_details(
    order_id: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Retrieves verified fulfillment status, line items, and totals for a customer order.

    Queries the persistent OrderGuard order database for a specific customer order ID,
    validates both input arguments and output payload against strict JSON schemas, and
    caches the active order metadata in session state for downstream policy checks.

    Args:
        order_id: Customer order identifier in 'ORD-XXXX' format (for example, 'ORD-1001').

    Returns:
        Dictionary conforming to `OrderDetailsOutput` with `status` ('success' or 'error'),
        order fulfillment metadata, item list, or a guided `recovery_instruction` if invalid.
    """
    normalized_id = (order_id or "").strip().upper()
    try:
        validated_in = OrderLookupInput(order_id=normalized_id)
    except ValidationError as exc:
        return OrderDetailsOutput(
            status="error",
            order_id=normalized_id or "UNKNOWN",
            error_code="INVALID_ORDER_ID_FORMAT",
            error_message=f"Order ID '{order_id}' does not match required pattern 'ORD-XXXX'.",
            recovery_instruction=format_validation_recovery(
                exc, "lookup_customer_order_details"
            ),
        ).model_dump()

    order = get_order_store().get_order(validated_in.order_id)
    if order is None:
        return OrderDetailsOutput(
            status="error",
            order_id=validated_in.order_id,
            error_code="ORDER_NOT_FOUND",
            error_message=f"No order record exists for '{validated_in.order_id}'.",
            recovery_instruction=(
                f"Order '{validated_in.order_id}' was not found in the database. "
                "Ask the customer to double-check their confirmation receipt for a valid "
                "order ID (such as 'ORD-1001' through 'ORD-1005') before retrying."
            ),
        ).model_dump()

    if tool_context is not None:
        tool_context.state["active_order_id"] = order["order_id"]
        tool_context.state["customer_tier"] = order["customer_tier"]
        tool_context.state["user:customer_tier"] = order["customer_tier"]

    output = OrderDetailsOutput(
        status="success",
        order_id=order["order_id"],
        customer_tier=order["customer_tier"],
        fulfillment_status=order["fulfillment_status"],
        order_date_iso=order["order_date_iso"],
        days_since_delivery=order["days_since_delivery"],
        total_amount_usd=order["total_amount_usd"],
        items=order["items"],
    )
    return output.model_dump()


def evaluate_refund_policy_eligibility(
    order_id: str,
    reason: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Evaluates whether an order qualifies for a refund under enterprise return policies.

    Checks fulfillment status, prior refund ledger entries, non-refundable merchandise
    categories (`digital_gift_card`), standard 30-day return windows, and defective/damaged
    window extensions (45 days standard, 60 days enterprise tier).

    Args:
        order_id: Customer order identifier in 'ORD-XXXX' format (for example, 'ORD-1001').
        reason: Normalized refund reason. Must be one of: 'defective_item',
            'damaged_in_shipping', 'wrong_item_sent', 'changed_mind', or 'late_delivery'.

    Returns:
        Dictionary conforming to `RefundEligibilityOutput` specifying `eligible`,
        `policy_code`, `refundable_amount_usd`, `requires_human_approval`, and explanation.
    """
    normalized_id = (order_id or "").strip().upper()
    normalized_reason = (reason or "").strip().lower()

    try:
        validated_in = RefundEligibilityInput(
            order_id=normalized_id,
            reason=normalized_reason,  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        return RefundEligibilityOutput(
            status="error",
            order_id=normalized_id or "UNKNOWN",
            eligible=False,
            policy_code="SCHEMA_VALIDATION_FAILED",
            refundable_amount_usd=0.0,
            requires_human_approval=False,
            policy_explanation="Input parameters failed strict schema validation.",
            error_code="INVALID_POLICY_EVALUATION_INPUT",
            error_message=str(exc),
            recovery_instruction=format_validation_recovery(
                exc, "evaluate_refund_policy_eligibility"
            ),
        ).model_dump()

    store = get_order_store()
    order = store.get_order(validated_in.order_id)
    if order is None:
        return RefundEligibilityOutput(
            status="error",
            order_id=validated_in.order_id,
            eligible=False,
            policy_code="ORDER_NOT_FOUND",
            refundable_amount_usd=0.0,
            requires_human_approval=False,
            policy_explanation=f"Order '{validated_in.order_id}' does not exist.",
            error_code="ORDER_NOT_FOUND",
            error_message=f"Cannot evaluate policy for unknown order '{validated_in.order_id}'.",
            recovery_instruction=(
                "Verify the customer's order ID using `lookup_customer_order_details` "
                "or ask the customer for a valid 'ORD-XXXX' order number."
            ),
        ).model_dump()

    existing_refund = store.get_existing_refund(validated_in.order_id)
    if existing_refund is not None or order["fulfillment_status"] == "REFUNDED":
        return RefundEligibilityOutput(
            status="success",
            order_id=validated_in.order_id,
            eligible=False,
            policy_code="ALREADY_REFUNDED",
            refundable_amount_usd=0.0,
            requires_human_approval=False,
            policy_explanation=(
                f"Order {validated_in.order_id} has already been refunded. Duplicate "
                "refunds are prohibited."
            ),
        ).model_dump()

    if order["fulfillment_status"] in {"PROCESSING", "IN_TRANSIT"}:
        return RefundEligibilityOutput(
            status="success",
            order_id=validated_in.order_id,
            eligible=False,
            policy_code="ORDER_STILL_IN_TRANSIT",
            refundable_amount_usd=0.0,
            requires_human_approval=False,
            policy_explanation=(
                f"Order {validated_in.order_id} is currently {order['fulfillment_status']}. "
                "Refunds can only be issued after delivery, though a specialist ticket "
                "can be opened via `create_priority_escalation_ticket`."
            ),
        ).model_dump()

    categories = {item["category"] for item in order["items"]}
    if "digital_gift_card" in categories:
        return RefundEligibilityOutput(
            status="success",
            order_id=validated_in.order_id,
            eligible=False,
            policy_code="FINAL_SALE_DIGITAL_GIFT_CARD",
            refundable_amount_usd=0.0,
            requires_human_approval=False,
            policy_explanation=(
                "Digital gift cards are final-sale items and are non-refundable under "
                "enterprise return policy."
            ),
        ).model_dump()

    settings = get_settings()
    days = int(order["days_since_delivery"] or 0)
    defect_reasons = {"defective_item", "damaged_in_shipping", "wrong_item_sent"}
    max_window = settings.standard_return_window_days
    if validated_in.reason in defect_reasons:
        max_window = 60 if order["customer_tier"] == "enterprise" else 45

    if days > max_window:
        return RefundEligibilityOutput(
            status="success",
            order_id=validated_in.order_id,
            eligible=False,
            policy_code="RETURN_WINDOW_EXPIRED",
            refundable_amount_usd=0.0,
            requires_human_approval=False,
            policy_explanation=(
                f"Order {validated_in.order_id} was delivered {days} days ago, exceeding "
                f"the {max_window}-day policy window for reason '{validated_in.reason}'."
            ),
        ).model_dump()

    amount = float(order["total_amount_usd"])
    needs_hitl = amount >= settings.human_approval_refund_threshold_usd

    if tool_context is not None:
        verified_map = dict(tool_context.state.get("verified_eligible_orders") or {})
        verified_map[validated_in.order_id] = {
            "refundable_amount_usd": amount,
            "reason": validated_in.reason,
            "requires_human_approval": needs_hitl,
        }
        tool_context.state["verified_eligible_orders"] = verified_map
        tool_context.state["active_order_id"] = validated_in.order_id

    return RefundEligibilityOutput(
        status="success",
        order_id=validated_in.order_id,
        eligible=True,
        policy_code="ELIGIBLE_VERIFIED_POLICY",
        refundable_amount_usd=amount,
        requires_human_approval=needs_hitl,
        policy_explanation=(
            f"Order {validated_in.order_id} ({days} days post-delivery) qualifies for a "
            f"full refund of ${amount:.2f} under reason '{validated_in.reason}'."
        ),
    ).model_dump()


def requires_human_refund_approval(
    refund_amount_usd: float = 0.0, **kwargs: Any
) -> bool:
    """Explicit Human-in-the-Loop predicate pausing high-stakes refunds for confirmation."""
    try:
        amount = float(refund_amount_usd)
    except (TypeError, ValueError):
        return True
    return amount >= get_settings().human_approval_refund_threshold_usd


def issue_verified_customer_refund(
    order_id: str,
    refund_amount_usd: float,
    reason: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Disburses a policy-verified refund to the customer's original payment method.

    High-stakes financial action protected by:
    1. ADK's `FunctionTool(..., require_confirmation=requires_human_refund_approval)`
       human-in-the-loop confirmation stop for refunds at or above threshold,
    2. Strict Pydantic schema validation and duplicate-disbursement checks,
    3. HMAC-SHA256 audit signing using keys managed via Google Cloud Secret Manager.

    Args:
        order_id: Verified order identifier in 'ORD-XXXX' format (for example, 'ORD-1001').
        refund_amount_usd: Positive USD refund amount matching the policy eligibility check.
        reason: Verified reason code ('defective_item', 'damaged_in_shipping',
            'wrong_item_sent', 'changed_mind', or 'late_delivery').

    Returns:
        Dictionary conforming to `RefundReceiptOutput` with `refund_id`, `signature_digest`,
        and `refunded_amount_usd`, or a guided `recovery_instruction` on policy/input error.
    """
    normalized_id = (order_id or "").strip().upper()
    normalized_reason = (reason or "").strip().lower()

    try:
        validated_in = IssueRefundInput(
            order_id=normalized_id,
            refund_amount_usd=refund_amount_usd,
            reason=normalized_reason,  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        return RefundReceiptOutput(
            status="error",
            order_id=normalized_id or "UNKNOWN",
            refunded_amount_usd=0.0,
            message="Refund request rejected due to invalid input schema.",
            error_code="INVALID_REFUND_INPUT",
            recovery_instruction=format_validation_recovery(
                exc, "issue_verified_customer_refund"
            ),
        ).model_dump()

    eligibility = evaluate_refund_policy_eligibility(
        order_id=validated_in.order_id,
        reason=validated_in.reason,
        tool_context=None,
    )
    if eligibility["status"] != "success" or not eligibility["eligible"]:
        return RefundReceiptOutput(
            status="error",
            order_id=validated_in.order_id,
            refunded_amount_usd=0.0,
            message=eligibility["policy_explanation"],
            error_code=eligibility["policy_code"],
            recovery_instruction=(
                f"Do NOT retry `issue_verified_customer_refund` for {validated_in.order_id} "
                f"because policy check returned '{eligibility['policy_code']}'. Explain the "
                "policy decision to the customer and offer `create_priority_escalation_ticket` "
                "if they require human specialist review."
            ),
        ).model_dump()

    expected_amount = float(eligibility["refundable_amount_usd"])
    if abs(validated_in.refund_amount_usd - expected_amount) > 0.01:
        return RefundReceiptOutput(
            status="error",
            order_id=validated_in.order_id,
            refunded_amount_usd=0.0,
            message=(
                f"Requested refund ${validated_in.refund_amount_usd:.2f} does not match "
                f"verified policy amount ${expected_amount:.2f}."
            ),
            error_code="REFUND_AMOUNT_MISMATCH",
            recovery_instruction=(
                f"Retry `issue_verified_customer_refund` using the exact verified "
                f"`refund_amount_usd` of {expected_amount:.2f}."
            ),
        ).model_dump()

    receipt = get_order_store().record_refund(
        order_id=validated_in.order_id,
        refund_amount_usd=validated_in.refund_amount_usd,
        reason=validated_in.reason,
    )

    if tool_context is not None:
        issued = list(tool_context.state.get("issued_refund_ids") or [])
        issued.append(receipt["refund_id"])
        tool_context.state["issued_refund_ids"] = issued

    return RefundReceiptOutput(
        status="success",
        order_id=validated_in.order_id,
        refund_id=receipt["refund_id"],
        refunded_amount_usd=receipt["refunded_amount_usd"],
        signature_digest=receipt["signature_digest"],
        message=(
            f"Refund {receipt['refund_id']} for ${receipt['refunded_amount_usd']:.2f} "
            f"on order {validated_in.order_id} has been issued."
        ),
    ).model_dump()


def requires_human_p1_escalation_approval(priority: str = "P3", **kwargs: Any) -> bool:
    """Requires human confirmation before paging on-call fraud/safety responders (`P1`)."""
    return str(priority).strip().upper() == "P1"


def create_priority_escalation_ticket(
    order_id: str,
    priority: str,
    issue_summary: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Creates a PII-scrubbed escalation ticket for specialist human support review.

    Used when an order is still in transit, outside standard policy windows, involves
    suspected billing fraud, or requires manual exception handling by a support lead.

    Args:
        order_id: Associated order identifier in 'ORD-XXXX' format (for example, 'ORD-1003').
        priority: Escalation urgency level: 'P1' (fraud/safety, 2h SLA), 'P2' (damaged or
            high-value dispute, 8h SLA), or 'P3' (standard policy exception review, 24h SLA).
        issue_summary: Concise summary (10-500 characters) describing the customer issue.

    Returns:
        Dictionary conforming to `EscalationTicketOutput` containing `ticket_id`,
        `priority`, `sla_hours`, and confirmation message, or guided error details.
    """
    normalized_id = (order_id or "").strip().upper()
    normalized_priority = (priority or "").strip().upper()
    scrubbed_summary = get_pii_redactor().redact_text(issue_summary or "")

    try:
        validated_in = EscalationTicketInput(
            order_id=normalized_id,
            priority=normalized_priority,  # type: ignore[arg-type]
            issue_summary=scrubbed_summary,
        )
    except ValidationError as exc:
        return EscalationTicketOutput(
            status="error",
            order_id=normalized_id or "UNKNOWN",
            priority=normalized_priority or "P3",
            message="Failed to create escalation ticket due to invalid arguments.",
            error_code="INVALID_ESCALATION_INPUT",
            recovery_instruction=format_validation_recovery(
                exc, "create_priority_escalation_ticket"
            ),
        ).model_dump()

    if get_order_store().get_order(validated_in.order_id) is None:
        return EscalationTicketOutput(
            status="error",
            order_id=validated_in.order_id,
            priority=validated_in.priority,
            message=f"Cannot escalate unknown order '{validated_in.order_id}'.",
            error_code="ORDER_NOT_FOUND",
            recovery_instruction=(
                "Confirm the customer's valid 'ORD-XXXX' order ID via "
                "`lookup_customer_order_details` before creating an escalation ticket."
            ),
        ).model_dump()

    ticket = get_order_store().create_ticket(
        order_id=validated_in.order_id,
        priority=validated_in.priority,
        issue_summary=validated_in.issue_summary,
    )
    if tool_context is not None:
        tool_context.state["last_escalation_ticket_id"] = ticket["ticket_id"]

    return EscalationTicketOutput(
        status="success",
        ticket_id=ticket["ticket_id"],
        order_id=ticket["order_id"],
        priority=ticket["priority"],
        sla_hours=ticket["sla_hours"],
        message=(
            f"Escalation ticket {ticket['ticket_id']} ({ticket['priority']}) created for "
            f"order {ticket['order_id']}. A specialist will respond within "
            f"{ticket['sla_hours']} hours."
        ),
    ).model_dump()


lookup_order_tool = FunctionTool(func=lookup_customer_order_details)
evaluate_refund_eligibility_tool = FunctionTool(func=evaluate_refund_policy_eligibility)
issue_refund_hitl_tool = FunctionTool(
    func=issue_verified_customer_refund,
    require_confirmation=requires_human_refund_approval,
)
create_escalation_ticket_tool = FunctionTool(
    func=create_priority_escalation_ticket,
    require_confirmation=requires_human_p1_escalation_approval,
)
