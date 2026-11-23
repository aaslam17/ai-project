"""Centralized runtime configuration for the OrderGuard ADK agent."""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path

from app.secrets import get_secret_resolver


@dataclass(frozen=True)
class AgentSettings:
    """Immutable configuration resolved from environment and GCP Secret Manager."""

    app_name: str
    root_agent_name: str
    project_id: str | None
    location: str
    fast_model_name: str
    planner_model_name: str
    order_store_db_path: str
    session_db_url: str
    human_approval_refund_threshold_usd: float
    max_auto_policy_refund_usd: float
    standard_return_window_days: int
    compaction_interval_events: int
    compaction_overlap_events: int
    otel_service_name: str
    enable_cloud_trace_export: bool
    enable_cloud_dlp: bool
    refund_signing_secret_id: str
    db_password_secret_id: str

    def get_refund_signing_key(self) -> str:
        """Resolves the HMAC signing key for refund receipts via Secret Manager."""
        resolved = get_secret_resolver().resolve_secret(
            secret_id=self.refund_signing_secret_id,
            env_var_name="REFUND_SIGNING_KEY",
            allow_ephemeral_fallback=True,
        )
        return resolved or "ephemeral-fallback-disabled"


@functools.lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    """Loads settings from environment variables with safe defaults."""
    base_adk_dir = Path(os.environ.get("ORDERGUARD_DATA_DIR", ".adk"))
    base_adk_dir.mkdir(parents=True, exist_ok=True)

    default_order_db = str(base_adk_dir / "orderguard_store.db")
    default_session_url = (
        f"sqlite+aiosqlite:///{(base_adk_dir / 'orderguard_sessions.db').as_posix()}"
    )

    return AgentSettings(
        app_name=os.environ.get("AGENT_APP_NAME", "app"),
        root_agent_name=os.environ.get("ROOT_AGENT_NAME", "ai_project"),
        project_id=os.environ.get("GOOGLE_CLOUD_PROJECT"),
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", "global"),
        fast_model_name=os.environ.get("FAST_MODEL_NAME", "gemini-3.8-flash"),
        planner_model_name=os.environ.get(
            "PLANNER_MODEL_NAME", "gemini-3.1-pro-preview"
        ),
        order_store_db_path=os.environ.get("ORDER_STORE_DB_PATH", default_order_db),
        session_db_url=os.environ.get("SESSION_DB_URL", default_session_url),
        human_approval_refund_threshold_usd=float(
            os.environ.get("HUMAN_APPROVAL_REFUND_THRESHOLD_USD", "50.0")
        ),
        max_auto_policy_refund_usd=float(
            os.environ.get("MAX_AUTO_POLICY_REFUND_USD", "1000.0")
        ),
        standard_return_window_days=int(
            os.environ.get("STANDARD_RETURN_WINDOW_DAYS", "30")
        ),
        compaction_interval_events=int(
            os.environ.get("COMPACTION_INTERVAL_EVENTS", "12")
        ),
        compaction_overlap_events=int(
            os.environ.get("COMPACTION_OVERLAP_EVENTS", "3")
        ),
        otel_service_name=os.environ.get("OTEL_SERVICE_NAME", "ai-project"),
        enable_cloud_trace_export=(
            os.environ.get("ENABLE_CLOUD_TRACE_EXPORT", "false").lower() == "true"
        ),
        enable_cloud_dlp=(
            os.environ.get("ENABLE_CLOUD_DLP", "false").lower() == "true"
        ),
        refund_signing_secret_id=os.environ.get(
            "REFUND_SIGNING_SECRET_ID", "ai-project-refund-signing-key"
        ),
        db_password_secret_id=os.environ.get(
            "DB_PASSWORD_SECRET_ID", "ai-project-db-password"
        ),
    )
