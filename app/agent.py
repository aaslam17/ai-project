# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""OrderGuard Multi-Agent Orchestration & ADK Application Definition.

Implements:
- Coordinator + Sequential multi-agent architecture (`ai_project` ->
  `order_lookup_specialist` | `refund_resolution_workflow`),
- Strategic Gemini Flash vs. Gemini Pro model routing (`app.routing`),
- History compaction (`EventsCompactionConfig` with sliding window + token threshold
  + `LlmEventSummarizer`, `ContextCacheConfig`, and `ContextFilterPlugin`),
- Security, policy, and self-evaluation guardrails (`SecurityAndPolicyGuardrailPlugin`),
- Structured JSON logging & OpenTelemetry Intent/Outcome spans (`IntentOutcomeObservabilityPlugin`),
- Tool self-healing (`ReflectAndRetryToolPlugin`) and optional BigQuery Agent Analytics.
"""

from __future__ import annotations

import logging
import os

from google.adk.agents import Agent, SequentialAgent
from google.adk.apps.app import App, ContextCacheConfig, EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.plugins.bigquery_agent_analytics_plugin import (
    BigQueryAgentAnalyticsPlugin,
    BigQueryLoggerConfig,
)
from google.adk.plugins.context_filter_plugin import ContextFilterPlugin
from google.adk.plugins.reflect_retry_tool_plugin import ReflectAndRetryToolPlugin
from google.genai import types

from app.config import get_settings
from app.guardrails import SecurityAndPolicyGuardrailPlugin
from app.memory import (
    build_preload_memory_tool,
    initialize_session_context_callback,
    schedule_async_memory_consolidation,
)
from app.observability import (
    IntentOutcomeObservabilityPlugin,
    configure_observability,
    emit_structured_event,
)
from app.prompts import (
    COORDINATOR_SYSTEM_CONSTITUTION,
    ORDER_LOOKUP_SPECIALIST_CONSTITUTION,
    REFUND_EXECUTION_SPECIALIST_CONSTITUTION,
    REFUND_POLICY_AUDITOR_CONSTITUTION,
)
from app.routing import get_model_router
from app.tools import (
    create_escalation_ticket_tool,
    evaluate_refund_eligibility_tool,
    issue_refund_hitl_tool,
    lookup_order_tool,
)

configure_observability()
_settings = get_settings()
_router = get_model_router()

# 1. Fast Operational Specialist (Gemini Flash) for order status & tracking inquiries
order_lookup_specialist = Agent(
    name="order_lookup_specialist",
    model=_router.build_gemini_model("fast_operational"),
    description=(
        "Fast fulfillment specialist for checking order status, delivery dates, "
        "line items, and purchase totals when no refund or dispute is requested."
    ),
    instruction=ORDER_LOOKUP_SPECIALIST_CONSTITUTION,
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[lookup_order_tool],
    before_agent_callback=initialize_session_context_callback,
)

# 2. Sequential Refund & Dispute Pipeline: Stage A (Pro Auditor) -> Stage B (Flash Executor)
refund_policy_auditor = Agent(
    name="refund_policy_auditor",
    model=_router.build_gemini_model("complex_planning"),
    description="Audits order records against return windows and refund eligibility rules.",
    instruction=REFUND_POLICY_AUDITOR_CONSTITUTION,
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[lookup_order_tool, evaluate_refund_eligibility_tool],
    output_key="policy_audit_summary",
    before_agent_callback=initialize_session_context_callback,
)

refund_execution_specialist = Agent(
    name="refund_execution_specialist",
    model=_router.build_gemini_model("fast_operational"),
    description=(
        "Executes verified customer refunds (with Human-in-the-Loop approval on "
        "high-stakes amounts) or creates priority escalation tickets."
    ),
    instruction=REFUND_EXECUTION_SPECIALIST_CONSTITUTION,
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[issue_refund_hitl_tool, create_escalation_ticket_tool],
    before_agent_callback=initialize_session_context_callback,
)

refund_resolution_workflow = SequentialAgent(
    name="refund_resolution_workflow",
    description=(
        "Deterministic sequential workflow for refund requests, damaged or defective "
        "merchandise claims, policy disputes, and support escalations."
    ),
    sub_agents=[refund_policy_auditor, refund_execution_specialist],
)

# 3. Root Coordinator Agent (keep name='ai_project' synced with agents-cli-manifest.yaml)
root_agent = Agent(
    name=_settings.root_agent_name,
    model=_router.build_gemini_model("complex_planning"),
    description=(
        "OrderGuard Principal Enterprise Customer Order & Refund Resolution Coordinator."
    ),
    instruction=COORDINATOR_SYSTEM_CONSTITUTION,
    generate_content_config=types.GenerateContentConfig(temperature=0.0),
    tools=[build_preload_memory_tool()],
    sub_agents=[order_lookup_specialist, refund_resolution_workflow],
    before_agent_callback=initialize_session_context_callback,
    after_agent_callback=schedule_async_memory_consolidation,
)


def _build_plugins() -> list[BasePlugin]:
    """Constructs the ordered ADK plugin stack for security, retries, context, and telemetry."""
    plugins: list[BasePlugin] = [
        SecurityAndPolicyGuardrailPlugin(),
        IntentOutcomeObservabilityPlugin(),
        ReflectAndRetryToolPlugin(
            max_retries=2,
            throw_exception_if_retry_exceeded=False,
        ),
        ContextFilterPlugin(num_invocations_to_keep=20),
    ]

    project_id = _settings.project_id
    enable_bq = os.environ.get("ENABLE_BQ_ANALYTICS", "false").lower() == "true"
    if project_id and enable_bq:
        dataset_id = os.environ.get("BQ_ANALYTICS_DATASET_ID", "adk_agent_analytics")
        location = os.environ.get("BQ_ANALYTICS_LOCATION", "us-central1")
        try:
            from google.cloud import bigquery

            bq_client = bigquery.Client(project=project_id)
            bq_client.create_dataset(f"{project_id}.{dataset_id}", exists_ok=True)
            plugins.append(
                BigQueryAgentAnalyticsPlugin(
                    project_id=project_id,
                    dataset_id=dataset_id,
                    location=location,
                    config=BigQueryLoggerConfig(
                        gcs_bucket_name=os.environ.get("BQ_ANALYTICS_GCS_BUCKET"),
                        connection_id=os.environ.get("BQ_ANALYTICS_CONNECTION_ID"),
                    ),
                )
            )
        except Exception as exc:
            emit_structured_event(
                "BQ_ANALYTICS_INIT_SKIPPED",
                f"Skipped BigQueryAgentAnalyticsPlugin initialization: {type(exc).__name__}",
                level=logging.WARNING,
                metadata={"detail": str(exc)},
            )

    return plugins


app = App(
    name=_settings.app_name,
    root_agent=root_agent,
    plugins=_build_plugins(),
    events_compaction_config=EventsCompactionConfig(
        summarizer=LlmEventSummarizer(
            llm=_router.build_gemini_model("fast_operational")
        ),
        compaction_interval=_settings.compaction_interval_events,
        overlap_size=_settings.compaction_overlap_events,
        token_threshold=8000,
        event_retention_size=6,
    ),
    context_cache_config=ContextCacheConfig(
        min_tokens=4096,
        ttl_seconds=1800,
        cache_intervals=10,
    ),
)
