"""Structured JSON Logging, OpenTelemetry Distributed Tracing, and Intent/Outcome Plugin.

Implements zero-print structured JSON telemetry correlated with OpenTelemetry trace
and span IDs, active PII scrubbing via `PiiRedactingLogFilter`, and explicit capture
of `INTENT` (before execution) vs. `OUTCOME` (after execution) across agents and tools.
"""

from __future__ import annotations

import collections
import logging
import sys
import time
import uuid
from typing import TYPE_CHECKING, Any

from google.adk.plugins.base_plugin import BasePlugin
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
)
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import Status, StatusCode
from pythonjsonlogger.json import JsonFormatter

from app.config import get_settings
from app.pii_redaction import PiiRedactingLogFilter, get_pii_redactor

if TYPE_CHECKING:
    from google.adk.agents.base_agent import BaseAgent
    from google.adk.agents.callback_context import CallbackContext
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext
    from google.genai import types

_LOGGER_NAME = "orderguard.telemetry"
_CONFIGURED = False
_IN_MEMORY_SPAN_EXPORTER = InMemorySpanExporter()
_RECENT_LOG_RECORDS: collections.deque[dict[str, Any]] = collections.deque(maxlen=250)


class CloudTraceCorrelatedJsonFormatter(JsonFormatter):
    """Formats log records as Cloud Logging–compatible structured JSON with OTel trace context."""

    def add_fields(
        self,
        log_record: dict[str, Any],
        record: logging.LogRecord,
        message_dict: dict[str, Any],
    ) -> None:
        super().add_fields(log_record, record, message_dict)
        log_record["severity"] = record.levelname
        log_record["logger"] = record.name

        current_span = trace.get_current_span()
        span_ctx = current_span.get_span_context() if current_span else None
        if span_ctx and span_ctx.is_valid:
            trace_hex = format(span_ctx.trace_id, "032x")
            span_hex = format(span_ctx.span_id, "016x")
            project_id = get_settings().project_id
            if project_id:
                log_record["logging.googleapis.com/trace"] = (
                    f"projects/{project_id}/traces/{trace_hex}"
                )
            log_record["trace_id"] = trace_hex
            log_record["logging.googleapis.com/spanId"] = span_hex
            log_record["span_id"] = span_hex

        _RECENT_LOG_RECORDS.append(dict(log_record))


def configure_observability() -> trace.Tracer:
    """Initializes structured JSON logging and OpenTelemetry TracerProvider idempotently."""
    global _CONFIGURED
    settings = get_settings()

    if not _CONFIGURED:
        resource = Resource.create(
            {
                "service.name": settings.otel_service_name,
                "service.namespace": "enterprise-support",
                "service.version": "0.1.0",
            }
        )
        current_provider = trace.get_tracer_provider()
        if not isinstance(current_provider, TracerProvider):
            provider = TracerProvider(resource=resource)
            provider.add_span_processor(SimpleSpanProcessor(_IN_MEMORY_SPAN_EXPORTER))
            if settings.enable_cloud_trace_export and settings.project_id:
                try:
                    from opentelemetry.exporter.cloud_trace import (
                        CloudTraceSpanExporter,
                    )

                    provider.add_span_processor(
                        BatchSpanProcessor(
                            CloudTraceSpanExporter(project_id=settings.project_id)
                        )
                    )
                except Exception:
                    pass
            trace.set_tracer_provider(provider)
        elif hasattr(current_provider, "add_span_processor"):
            current_provider.add_span_processor(
                SimpleSpanProcessor(_IN_MEMORY_SPAN_EXPORTER)
            )

        logger = logging.getLogger(_LOGGER_NAME)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            formatter = CloudTraceCorrelatedJsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                rename_fields={"asctime": "timestamp"},
            )
            handler.setFormatter(formatter)
            handler.addFilter(PiiRedactingLogFilter())
            logger.addHandler(handler)

        _CONFIGURED = True

    return trace.get_tracer(settings.otel_service_name)


def get_structured_logger() -> logging.Logger:
    """Returns the PII-scrubbed structured JSON logger."""
    configure_observability()
    return logging.getLogger(_LOGGER_NAME)


def get_tracer() -> trace.Tracer:
    """Returns the OpenTelemetry tracer for OrderGuard."""
    return configure_observability()


def get_recent_structured_logs() -> list[dict[str, Any]]:
    """Returns recent structured JSON log records (for diagnostics and test assertions)."""
    return list(_RECENT_LOG_RECORDS)


def clear_recent_telemetry() -> None:
    """Clears buffered in-memory log records and spans for isolated unit testing."""
    _RECENT_LOG_RECORDS.clear()
    _IN_MEMORY_SPAN_EXPORTER.clear()


def get_exported_spans() -> list[Any]:
    """Returns finished OpenTelemetry spans captured in memory."""
    return list(_IN_MEMORY_SPAN_EXPORTER.get_finished_spans())


def emit_structured_event(
    event_type: str,
    message: str,
    *,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Emits a PII-scrubbed structured JSON event with rich metadata."""
    redactor = get_pii_redactor()
    scrubbed_fields = redactor.scrub_structure(fields)
    scrubbed_message = redactor.redact_text(message)
    logger = get_structured_logger()
    logger.log(
        level,
        scrubbed_message,
        extra={"event_type": event_type, **scrubbed_fields},
    )


class IntentOutcomeObservabilityPlugin(BasePlugin):
    """ADK Plugin recording linked OpenTelemetry spans and explicit Intent vs. Outcome logs."""

    def __init__(self, name: str = "intent_outcome_observability_plugin") -> None:
        super().__init__(name=name)
        self._active_agent_spans: dict[str, tuple[Any, float, str]] = {}
        self._active_tool_spans: dict[str, tuple[Any, float, str]] = {}

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        tracer = get_tracer()
        correlation_id = f"agt-{uuid.uuid4().hex[:10]}"
        invocation_id = getattr(callback_context, "invocation_id", "unknown")
        session_id = getattr(callback_context.session, "id", "unknown")
        key = f"{invocation_id}:{agent.name}"

        span = tracer.start_span(
            f"agent.turn.{agent.name}",
            attributes={
                "gen_ai.agent.name": agent.name,
                "orderguard.session_id": str(session_id),
                "orderguard.invocation_id": str(invocation_id),
                "orderguard.correlation_id": correlation_id,
            },
        )
        self._active_agent_spans[key] = (span, time.perf_counter(), correlation_id)

        with trace.use_span(span, end_on_exit=False):
            emit_structured_event(
                "AGENT_INTENT",
                f"Agent '{agent.name}' starting turn with intended delegation/resolution.",
                phase="INTENT",
                agent_name=agent.name,
                session_id=str(session_id),
                invocation_id=str(invocation_id),
                correlation_id=correlation_id,
                intended_action=f"execute_agent:{agent.name}",
            )
        return None

    async def after_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> types.Content | None:
        invocation_id = getattr(callback_context, "invocation_id", "unknown")
        session_id = getattr(callback_context.session, "id", "unknown")
        key = f"{invocation_id}:{agent.name}"
        entry = self._active_agent_spans.pop(key, None)

        if entry is not None:
            span, started_at, correlation_id = entry
            duration_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
            span.set_attribute("orderguard.duration_ms", duration_ms)
            span.set_status(Status(StatusCode.OK))
            with trace.use_span(span, end_on_exit=True):
                emit_structured_event(
                    "AGENT_OUTCOME",
                    f"Agent '{agent.name}' completed turn.",
                    phase="OUTCOME",
                    agent_name=agent.name,
                    session_id=str(session_id),
                    invocation_id=str(invocation_id),
                    correlation_id=correlation_id,
                    actual_outcome="completed",
                    duration_ms=duration_ms,
                )
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        tracer = get_tracer()
        correlation_id = f"tool-{uuid.uuid4().hex[:10]}"
        call_id = getattr(tool_context, "function_call_id", None) or correlation_id
        session_id = getattr(tool_context.session, "id", "unknown")

        span = tracer.start_span(
            f"tool.execute.{tool.name}",
            attributes={
                "gen_ai.tool.name": tool.name,
                "orderguard.session_id": str(session_id),
                "orderguard.correlation_id": correlation_id,
            },
        )
        self._active_tool_spans[str(call_id)] = (
            span,
            time.perf_counter(),
            correlation_id,
        )

        with trace.use_span(span, end_on_exit=False):
            emit_structured_event(
                "TOOL_INTENT",
                f"Intended tool execution: '{tool.name}'.",
                phase="INTENT",
                tool_name=tool.name,
                session_id=str(session_id),
                correlation_id=correlation_id,
                intended_action=f"invoke_tool:{tool.name}",
                planned_args=tool_args,
            )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        call_id = getattr(tool_context, "function_call_id", None)
        entry = (
            self._active_tool_spans.pop(str(call_id), None)
            if call_id is not None
            else None
        )
        if entry is None and self._active_tool_spans:
            _, entry = self._active_tool_spans.popitem()

        session_id = getattr(tool_context.session, "id", "unknown")
        outcome_status = (
            result.get("status", "success") if isinstance(result, dict) else "success"
        )

        if entry is not None:
            span, started_at, correlation_id = entry
            duration_ms = round((time.perf_counter() - started_at) * 1000.0, 2)
            span.set_attribute("orderguard.duration_ms", duration_ms)
            span.set_attribute("orderguard.outcome_status", str(outcome_status))
            span.set_status(
                Status(
                    StatusCode.OK
                    if outcome_status != "error"
                    else StatusCode.ERROR
                )
            )
            with trace.use_span(span, end_on_exit=True):
                emit_structured_event(
                    "TOOL_OUTCOME",
                    f"Completed tool execution: '{tool.name}' with status='{outcome_status}'.",
                    phase="OUTCOME",
                    tool_name=tool.name,
                    session_id=str(session_id),
                    correlation_id=correlation_id,
                    actual_outcome=str(outcome_status),
                    duration_ms=duration_ms,
                    output_summary=result,
                )
        else:
            emit_structured_event(
                "TOOL_OUTCOME",
                f"Completed tool execution: '{tool.name}' with status='{outcome_status}'.",
                phase="OUTCOME",
                tool_name=tool.name,
                session_id=str(session_id),
                actual_outcome=str(outcome_status),
                output_summary=result,
            )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        call_id = getattr(tool_context, "function_call_id", None)
        entry = (
            self._active_tool_spans.pop(str(call_id), None)
            if call_id is not None
            else None
        )
        correlation_id = entry[2] if entry is not None else f"err-{uuid.uuid4().hex[:8]}"
        if entry is not None:
            span = entry[0]
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, str(error)))
            span.end()

        emit_structured_event(
            "TOOL_OUTCOME",
            f"Tool '{tool.name}' raised unhandled error: {type(error).__name__}.",
            level=logging.ERROR,
            phase="OUTCOME",
            tool_name=tool.name,
            correlation_id=correlation_id,
            actual_outcome="exception",
            planned_args=tool_args,
            output_summary={"error_type": type(error).__name__, "detail": str(error)},
        )
        return {
            "status": "error",
            "error_code": "UNEXPECTED_TOOL_EXCEPTION",
            "error_message": get_pii_redactor().redact_text(str(error)),
            "recovery_instruction": (
                f"Tool `{tool.name}` encountered an internal error. Verify the arguments "
                "match the required schema or offer to create an escalation ticket via "
                "`create_priority_escalation_ticket`."
            ),
        }
