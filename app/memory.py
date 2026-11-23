"""Persistent Session State, Context Initialization, and Async Memory Consolidation.

Connects OrderGuard to persistent database session storage (`DatabaseSessionService`
backed by Cloud SQL PostgreSQL in production or async SQLite locally), initializes
template variables safely on turn start, and runs PII-scrubbed memory generation and
consolidation asynchronously via `asyncio.create_task()` so UI turns never block.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from google.adk.memory import InMemoryMemoryService
from google.adk.sessions import DatabaseSessionService
from google.adk.tools.preload_memory_tool import PreloadMemoryTool

from app.config import get_settings
from app.observability import emit_structured_event
from app.pii_redaction import get_pii_redactor
from app.store import get_order_store

if TYPE_CHECKING:
    from google.adk.agents.callback_context import CallbackContext
    from google.genai import types

# Strong reference registry preventing background `asyncio.Task` garbage collection.
_BACKGROUND_MEMORY_TASKS: set[asyncio.Task[None]] = set()
_MEMORY_SERVICE = InMemoryMemoryService()


def create_persistent_session_service(db_url: str | None = None) -> DatabaseSessionService:
    """Creates a persistent SQL-backed ADK `DatabaseSessionService`."""
    resolved_url = db_url or get_settings().session_db_url
    return DatabaseSessionService(db_url=resolved_url)


def get_memory_service() -> InMemoryMemoryService:
    """Returns the configured ADK memory service."""
    return _MEMORY_SERVICE


def build_preload_memory_tool() -> PreloadMemoryTool:
    """Builds the ADK `PreloadMemoryTool` to inject relevant memories each turn."""
    return PreloadMemoryTool()


def get_pending_memory_tasks() -> set[asyncio.Task[None]]:
    """Returns currently running background memory consolidation tasks."""
    return set(_BACKGROUND_MEMORY_TASKS)


async def wait_for_background_memory_tasks(timeout: float = 5.0) -> None:
    """Awaits any in-flight background memory consolidation tasks (useful in tests)."""
    if not _BACKGROUND_MEMORY_TASKS:
        return
    await asyncio.wait(list(_BACKGROUND_MEMORY_TASKS), timeout=timeout)


async def initialize_session_context_callback(
    callback_context: CallbackContext,
) -> types.Content | None:
    """Initializes dynamic state keys before agent execution to prevent KeyError."""
    state = callback_context.state
    if "active_order_id" not in state:
        state["active_order_id"] = "none"
    if "customer_tier" not in state:
        state["customer_tier"] = state.get("user:customer_tier", "standard")
    if "policy_audit_summary" not in state:
        state["policy_audit_summary"] = "No policy audit run yet."
    if "verified_eligible_orders" not in state:
        state["verified_eligible_orders"] = {}

    user_id = getattr(callback_context.session, "user_id", "anonymous")
    prior_notes = get_order_store().list_memory_notes(str(user_id), limit=3)
    state["prior_memory_notes"] = (
        " | ".join(prior_notes) if prior_notes else "No prior history recorded."
    )
    return None


async def _consolidate_session_memory_async(
    *,
    user_id: str,
    session_id: str,
    app_name: str,
    raw_snippets: list[str],
    session_snapshot: Any,
) -> None:
    """Background worker that scrubs PII and consolidates session facts into memory."""
    redactor = get_pii_redactor()
    try:
        scrubbed_snippets = [
            redactor.redact_text(snippet) for snippet in raw_snippets if snippet.strip()
        ]
        if scrubbed_snippets:
            condensed = " ; ".join(scrubbed_snippets[-4:])[:600]
            note_id = await asyncio.to_thread(
                get_order_store().save_memory_note,
                user_id=user_id,
                session_id=session_id,
                summary=condensed,
            )
            emit_structured_event(
                "MEMORY_CONSOLIDATED",
                "Background PII-scrubbed memory consolidation succeeded.",
                phase="OUTCOME",
                user_id=user_id,
                session_id=session_id,
                app_name=app_name,
                metadata={"note_id": note_id, "snippet_count": len(scrubbed_snippets)},
            )

        if session_snapshot is not None:
            await _MEMORY_SERVICE.add_session_to_memory(session_snapshot)
    except Exception as exc:
        emit_structured_event(
            "MEMORY_CONSOLIDATION_ERROR",
            f"Async memory consolidation failed: {type(exc).__name__}",
            level=logging.WARNING,
            phase="OUTCOME",
            session_id=session_id,
            metadata={"error": str(exc)},
        )


async def schedule_async_memory_consolidation(
    callback_context: CallbackContext,
) -> types.Content | None:
    """Non-blocking `after_agent_callback` dispatching memory generation in the background."""
    session = callback_context.session
    user_id = str(getattr(session, "user_id", "anonymous"))
    session_id = str(getattr(session, "id", "unknown"))
    app_name = str(getattr(session, "app_name", get_settings().app_name))

    raw_snippets: list[str] = []
    for event in getattr(session, "events", [])[-6:]:
        content = getattr(event, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            continue
        for part in parts:
            text_val = getattr(part, "text", None)
            if text_val:
                raw_snippets.append(f"{getattr(event, 'author', 'turn')}: {text_val}")

    emit_structured_event(
        "MEMORY_CONSOLIDATION_SCHEDULED",
        "Scheduling non-blocking async memory consolidation task.",
        phase="INTENT",
        user_id=user_id,
        session_id=session_id,
        intended_action="consolidate_session_memory_async",
    )

    task = asyncio.create_task(
        _consolidate_session_memory_async(
            user_id=user_id,
            session_id=session_id,
            app_name=app_name,
            raw_snippets=raw_snippets,
            session_snapshot=None,
        ),
        name=f"orderguard-memory-{session_id}",
    )
    _BACKGROUND_MEMORY_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_MEMORY_TASKS.discard)
    return None
