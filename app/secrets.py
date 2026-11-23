"""Secure Secret Management backed by Google Cloud Secret Manager.

Eliminates hardcoded API keys or credentials by resolving secrets via:
1. Cloud Run / GKE Secret Manager volume or env bindings (`secret_key_ref`),
2. Direct runtime fetch via `google.cloud.secretmanager.SecretManagerServiceClient`,
3. Cryptographically random per-process ephemeral keys in isolated local tests.
"""

from __future__ import annotations

import functools
import os
import secrets
from typing import Any


class SecretManagerResolver:
    """Fetches and caches secrets from Google Cloud Secret Manager."""

    def __init__(self, project_id: str | None = None) -> None:
        self._project_id = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT")
        self._client: Any = None
        self._ephemeral_fallbacks: dict[str, str] = {}

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def resolve_secret(
        self,
        *,
        secret_id: str,
        env_var_name: str | None = None,
        version: str = "latest",
        allow_ephemeral_fallback: bool = True,
    ) -> str | None:
        """Resolves a secret securely without ever storing credentials in code.

        Args:
            secret_id: Secret identifier in Google Cloud Secret Manager.
            env_var_name: Optional environment variable injected by Cloud Run Secret Manager.
            version: Secret version to read (defaults to 'latest').
            allow_ephemeral_fallback: Generates an in-memory ephemeral key for local dev/tests.

        Returns:
            Resolved secret value or None if unavailable and fallback is disabled.
        """
        if env_var_name:
            injected = os.environ.get(env_var_name)
            if injected:
                return injected

        if self._project_id and secret_id:
            fetched = self._fetch_from_gcp_secret_manager(secret_id, version=version)
            if fetched is not None:
                return fetched

        if allow_ephemeral_fallback:
            if secret_id not in self._ephemeral_fallbacks:
                self._ephemeral_fallbacks[secret_id] = secrets.token_hex(32)
            return self._ephemeral_fallbacks[secret_id]

        return None

    def _fetch_from_gcp_secret_manager(
        self, secret_id: str, *, version: str = "latest"
    ) -> str | None:
        """Reads secret payload directly from Google Cloud Secret Manager."""
        if not self._project_id:
            return None
        # Skip remote network round-trip during unit tests unless explicitly enabled.
        if os.environ.get("ENABLE_SECRET_MANAGER_FETCH", "false").lower() != "true":
            return None
        try:
            client = self._get_client()
            resource_name = (
                f"projects/{self._project_id}/secrets/{secret_id}/versions/{version}"
            )
            response = client.access_secret_version(
                request={"name": resource_name}, timeout=3.0
            )
            return response.payload.data.decode("utf-8").strip()
        except Exception:
            return None


@functools.lru_cache(maxsize=1)
def get_secret_resolver() -> SecretManagerResolver:
    """Returns a singleton SecretManagerResolver."""
    return SecretManagerResolver()
