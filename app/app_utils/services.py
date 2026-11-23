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

"""Process-wide ADK session, memory, and artifact services shared by every serving surface.

Registered under ``shared://`` so the ADK web routes, the A2A path, and the
reasoning_engine adapter share one persistent instance:
- Production (Cloud Run): Cloud SQL PostgreSQL via `INSTANCE_CONNECTION_NAME` +
  Secret Manager–injected `DB_PASS`.
- Local / Standalone: Persistent async SQLite (`DatabaseSessionService`) so
  conversational state survives restarts without requiring external setup.
"""

from __future__ import annotations

import functools
import os
from urllib.parse import quote

from google.adk.artifacts import GcsArtifactService, InMemoryArtifactService
from google.adk.cli.service_registry import get_service_registry
from google.adk.cli.utils.service_factory import create_session_service_from_options
from google.adk.sessions import DatabaseSessionService, InMemorySessionService

from app.config import get_settings
from app.secrets import get_secret_resolver

SESSION_SERVICE_URI = "shared://session"
ARTIFACT_SERVICE_URI = "shared://artifact"

_AGENT_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)


@functools.cache
def get_session_service():
    """Returns a persistent SQL-backed ADK session service across turns."""
    settings = get_settings()
    db_user = os.environ.get("DB_USER", "postgres")
    db_name = os.environ.get("DB_NAME", "postgres")
    db_pass = get_secret_resolver().resolve_secret(
        secret_id=settings.db_password_secret_id,
        env_var_name="DB_PASS",
        allow_ephemeral_fallback=False,
    )
    instance_connection_name = os.environ.get("INSTANCE_CONNECTION_NAME")

    if instance_connection_name and db_pass:
        encoded_user = quote(db_user, safe="")
        encoded_pass = quote(db_pass, safe="")
        encoded_instance = instance_connection_name.replace(":", "%3A")
        session_service_uri = (
            f"postgresql+asyncpg://{encoded_user}:{encoded_pass}@"
            f"/{db_name}?host=/cloudsql/{encoded_instance}"
        )
        return create_session_service_from_options(
            base_dir=_AGENT_DIR, session_service_uri=session_service_uri
        )

    if os.environ.get("USE_IN_MEMORY_SESSIONS", "false").lower() == "true":
        return InMemorySessionService()

    return DatabaseSessionService(db_url=settings.session_db_url)


@functools.cache
def get_artifact_service():
    """Process-wide artifact service: GCS when a bucket is set, else in-memory."""
    if bucket := os.environ.get("LOGS_BUCKET_NAME"):
        return GcsArtifactService(bucket_name=bucket)
    return InMemoryArtifactService()


_registry = get_service_registry()
_registry.register_session_service("shared", lambda uri, **kw: get_session_service())
_registry.register_artifact_service("shared", lambda uri, **kw: get_artifact_service())
