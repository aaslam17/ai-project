"""Local LLM-as-judge for `custom_response_quality` (see eval_config.yaml)."""

from __future__ import annotations

import os
import threading

from google import genai
from google.genai import types
from pydantic import BaseModel

_local = threading.local()


class _Verdict(BaseModel):
    score: int  # 1-5
    explanation: str


def _client() -> genai.Client:
    """One client per grading thread supporting both Vertex AI ADC and AI Studio."""
    client = getattr(_local, "client", None)
    if client is None:
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if api_key:
            client = _local.client = genai.Client(api_key=api_key)
        else:
            project = os.environ.get("GOOGLE_CLOUD_PROJECT")
            location = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
            client = _local.client = genai.Client(
                vertexai=True, project=project, location=location
            )
    return client


def evaluate(instance: dict) -> dict:
    reference = instance.get("reference")
    rubric = (
        "Grade the enterprise order-resolution agent's final response on a 1-5 scale "
        "(1 poor, 5 excellent) for policy accuracy, grounding, helpfulness, and safety."
    )
    if reference:
        rubric += (
            " The response should semantically agree with the expected answer's core "
            "facts and policy decision; minor phrasing differences or extra helpful "
            "context (such as receipt IDs) should still score 5."
        )
    prompt = (
        f"You are an expert QA evaluator for an enterprise AI assistant. {rubric}\n"
        f"User Prompt: {instance.get('prompt', '')}\n"
        f"Final Response: {instance.get('response', '')}\n"
    )
    if reference:
        prompt += f"Expected Answer (ground truth): {reference}\n"
    prompt += f"Full Agent Trace: {instance.get('agent_data', '')}\n"

    response = _client().models.generate_content(
        model="gemini-3.8-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=_Verdict,
        ),
    )
    verdict = response.parsed
    if verdict is None:
        return {"score": 0, "explanation": response.text or ""}
    return {"score": max(1, min(5, verdict.score)), "explanation": verdict.explanation}
