"""Strategic Model Routing between low-latency Gemini Flash and reasoning Gemini Pro.

Routes fast operational tasks (order status lookup, action execution, event compaction)
to Gemini Flash, while routing complex multi-step coordination, policy compliance
auditing, and dispute analysis to Gemini Pro.
"""

from __future__ import annotations

from typing import Literal

from google.adk.models import Gemini
from google.genai import types

from app.config import AgentSettings, get_settings

TaskComplexityTier = Literal["fast_operational", "complex_planning"]

_COMPLEX_INTENT_KEYWORDS: tuple[str, ...] = (
    "refund",
    "damaged",
    "defective",
    "broken",
    "dispute",
    "fraud",
    "escalate",
    "policy",
    "exception",
    "wrong item",
    "missing",
)


class StrategicModelRouter:
    """Selects and instantiates the optimal Gemini model tier per agent role or query."""

    def __init__(self, settings: AgentSettings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def fast_model_name(self) -> str:
        return self._settings.fast_model_name

    @property
    def planner_model_name(self) -> str:
        return self._settings.planner_model_name

    def classify_query_complexity(self, user_query: str) -> TaskComplexityTier:
        """Classifies incoming user text into fast operational vs complex planning."""
        normalized = (user_query or "").lower()
        if any(keyword in normalized for keyword in _COMPLEX_INTENT_KEYWORDS):
            return "complex_planning"
        if len(normalized.split()) > 35:
            return "complex_planning"
        return "fast_operational"

    def resolve_model_name(self, tier: TaskComplexityTier) -> str:
        """Returns the configured Gemini model identifier for the given complexity tier."""
        if tier == "complex_planning":
            return self._settings.planner_model_name
        return self._settings.fast_model_name

    def build_gemini_model(self, tier: TaskComplexityTier) -> Gemini:
        """Constructs an ADK `Gemini` wrapper with retry configuration for `tier`."""
        return Gemini(
            model=self.resolve_model_name(tier),
            retry_options=types.HttpRetryOptions(
                attempts=3,
                initial_delay=1.0,
            ),
        )


def get_model_router() -> StrategicModelRouter:
    """Returns a configured StrategicModelRouter instance."""
    return StrategicModelRouter()
