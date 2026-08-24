"""
The concrete hallucination guardrail for the chat console (requirement:
"if the retrieved context doesn't contain data relevant to the
question, the response should say so rather than inventing numbers").

Two layers, deliberately in this order:
  1. PROMPT-LEVEL instruction (see prompt_templates.py) telling the
     model explicitly to say "I don't have data on that" rather than
     guess, when context is empty or doesn't cover the question.
  2. PROGRAMMATIC post-hoc check (this file): scans the model's response
     for any breaker_id/panel_id-shaped token, and flags (does NOT
     silently rewrite or block) any such token that wasn't in the
     retrieved context - i.e. the model mentioned a specific joint we
     never gave it data about, which is the single clearest, cheaply-
     detectable signature of a fabricated-detail hallucination in this
     domain.

Layer 1 alone is a request, not a guarantee - models don't always follow
instructions. Layer 2 doesn't require the model to comply with anything;
it's an independent, deterministic check against what data was ACTUALLY
retrieved. Flagged responses aren't discarded here (that decision -
retry, append a disclaimer, or just log for review - belongs to the
caller, chat_service.py) because a false positive (a joint ID appearing
in the model's response that happens to also appear in its OWN
system-prompt boilerplate, for instance) shouldn't silently eat a
perfectly good answer.
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GroundingCheckResult:
    ungrounded_ids: list[str]
    is_grounded: bool


def check_grounding(response_text: str, known_ids: set[str]) -> GroundingCheckResult:
    """
    known_ids: every panel_id and breaker_id that appeared in the
    context actually sent to the model for this call (build from
    app/layer5/retrieval.py's RetrievedContext before calling this).
    """
    mentioned = set(re.findall(r"\b[a-zA-Z]+-[a-zA-Z0-9]+(?:-[a-zA-Z0-9]+)*\b", response_text))
    ungrounded = sorted(mentioned - known_ids)
    return GroundingCheckResult(ungrounded_ids=ungrounded, is_grounded=len(ungrounded) == 0)
