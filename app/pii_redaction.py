"""Active PII Redaction pipeline for structured logging, session state, and memory.

Provides deterministic regex scrubbing for sensitive customer identifiers (emails,
payment card numbers, SSNs, phone numbers, bearer/API tokens) and optional Google
Cloud Sensitive Data Protection (`google-cloud-dlp`) de-identification before any
payload reaches logs or persistent storage.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

# Ordered from most-specific to broadest to avoid partial token collisions.
_PII_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # GitHub PATs, Google API keys, Bearer tokens
    (
        re.compile(
            r"\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z\-_]{30,})\b"
        ),
        "[REDACTED_SECRET]",
    ),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-._~+/]+=*"),
        "Bearer [REDACTED_TOKEN]",
    ),
    # Payment card numbers (13-19 digits with optional spaces or hyphens)
    (
        re.compile(r"\b(?:\d{4}[-\s]?){3}\d{1,7}\b"),
        "[REDACTED_PAYMENT_CARD]",
    ),
    # US Social Security Numbers (XXX-XX-XXXX)
    (
        re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "[REDACTED_SSN]",
    ),
    # Email addresses
    (
        re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED_EMAIL]",
    ),
    # North American & international phone numbers (preserve ORD-XXXX order IDs!)
    (
        re.compile(
            r"(?<!ORD-)(?:\+\d{1,3}[-\s.]?)?\(?\d{3}\)?[-\s.]\d{3}[-\s.]\d{4}\b"
        ),
        "[REDACTED_PHONE]",
    ),
)

_SENSITIVE_DICT_KEYS: frozenset[str] = frozenset(
    {
        "password",
        "db_pass",
        "api_key",
        "secret",
        "authorization",
        "credit_card",
        "card_number",
        "ssn",
        "cvv",
    }
)


class PiiRedactor:
    """Scrubs sensitive PII from text and nested structures prior to storage."""

    def __init__(
        self,
        *,
        project_id: str | None = None,
        enable_cloud_dlp: bool | None = None,
    ) -> None:
        self._project_id = project_id or os.environ.get("GOOGLE_CLOUD_PROJECT")
        if enable_cloud_dlp is None:
            enable_cloud_dlp = (
                os.environ.get("ENABLE_CLOUD_DLP", "false").lower() == "true"
            )
        self._enable_cloud_dlp = bool(enable_cloud_dlp and self._project_id)
        self._dlp_client: Any = None

    def _get_dlp_client(self) -> Any:
        if self._dlp_client is None and self._enable_cloud_dlp:
            from google.cloud import dlp_v2

            self._dlp_client = dlp_v2.DlpServiceClient()
        return self._dlp_client

    def redact_text(self, text: str) -> str:
        """Scrubs PII from a string using deterministic regex + optional Cloud DLP."""
        if not text:
            return text

        scrubbed = text
        for pattern, replacement in _PII_PATTERNS:
            scrubbed = pattern.sub(replacement, scrubbed)

        if self._enable_cloud_dlp and self._project_id:
            scrubbed = self._redact_via_cloud_dlp(scrubbed)

        return scrubbed

    def _redact_via_cloud_dlp(self, text: str) -> str:
        """Applies Google Cloud Sensitive Data Protection de-identification."""
        try:
            client = self._get_dlp_client()
            if client is None:
                return text

            info_types = [
                {"name": "EMAIL_ADDRESS"},
                {"name": "PHONE_NUMBER"},
                {"name": "CREDIT_CARD_NUMBER"},
                {"name": "US_SOCIAL_SECURITY_NUMBER"},
                {"name": "PERSON_NAME"},
            ]
            request = {
                "parent": f"projects/{self._project_id}/locations/global",
                "inspect_config": {"info_types": info_types},
                "deidentify_config": {
                    "info_type_transformations": {
                        "transformations": [
                            {"primitive_transformation": {"replace_with_info_type_config": {}}}
                        ]
                    }
                },
                "item": {"value": text},
            }
            response = client.deidentify_content(request=request, timeout=3.0)
            return response.item.value or text
        except Exception:
            # Never fail open on PII: deterministic regex scrubbing has already run.
            return text

    def scrub_structure(self, value: Any) -> Any:
        """Recursively scrubs strings and sensitive keys in dicts, lists, and tuples."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            cleaned: dict[Any, Any] = {}
            for key, item in value.items():
                key_str = str(key).lower()
                if key_str in _SENSITIVE_DICT_KEYS:
                    cleaned[key] = "[REDACTED_SECRET_FIELD]"
                else:
                    cleaned[key] = self.scrub_structure(item)
            return cleaned
        if isinstance(value, list):
            return [self.scrub_structure(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.scrub_structure(item) for item in value)
        return value

    def contains_pii(self, text: str) -> bool:
        """Returns True if raw text matches any known PII or secret pattern."""
        if not text:
            return False
        return any(pattern.search(text) is not None for pattern, _ in _PII_PATTERNS)


class PiiRedactingLogFilter(logging.Filter):
    """Logging filter ensuring all messages and extra structured fields are PII-free."""

    def __init__(self, redactor: PiiRedactor | None = None) -> None:
        super().__init__()
        self._redactor = redactor or get_pii_redactor()

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redactor.redact_text(record.msg)
        if record.args:
            record.args = self._redactor.scrub_structure(record.args)

        for attr_name in (
            "metadata",
            "planned_args",
            "output_summary",
            "intended_action",
            "actual_outcome",
        ):
            if hasattr(record, attr_name):
                setattr(
                    record,
                    attr_name,
                    self._redactor.scrub_structure(getattr(record, attr_name)),
                )
        return True


_DEFAULT_REDACTOR: PiiRedactor | None = None


def get_pii_redactor() -> PiiRedactor:
    """Returns the process-wide PII redactor instance."""
    global _DEFAULT_REDACTOR
    if _DEFAULT_REDACTOR is None:
        _DEFAULT_REDACTOR = PiiRedactor()
    return _DEFAULT_REDACTOR
