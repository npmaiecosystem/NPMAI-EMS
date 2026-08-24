"""
The structured JSON shape every audit/alert response must conform to,
per Layer 6/7's consumption contract. Validated with pydantic (already
used throughout this project for the same purpose - Layer 2's
TelemetryPayload - this is a validation library, not an LLM
orchestration framework, so it doesn't conflict with the "no wrappers"
requirement).
"""
import json
import logging
from typing import Optional

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class DiagnosticOutput(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    severity: str = Field(..., pattern="^(healthy|minor|intermediate|serious|emergency)$")
    plain_language_explanation: str = Field(..., min_length=1)
    recommended_action: str = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=1.0)


def parse_diagnostic_output(raw_text: str) -> Optional[DiagnosticOutput]:
    """
    Returns None (never raises) on malformed output - callers
    (audit_service.py) treat a None as "this audit cycle produced
    nothing usable" and log/skip, rather than crashing the whole
    scheduled run over one bad generation. Strips markdown code fences
    defensively, since models frequently wrap JSON in ```json blocks
    even when explicitly told not to (see prompt_templates.py's
    instruction) - stripping here is cheap insurance, not a substitute
    for that instruction.
    """
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        return DiagnosticOutput.model_validate(data)
    except (json.JSONDecodeError, ValidationError) as exc:
        logger.warning("Failed to parse/validate LLM output as DiagnosticOutput: %s\nRaw text was: %s", exc, raw_text[:1000])
        return None
