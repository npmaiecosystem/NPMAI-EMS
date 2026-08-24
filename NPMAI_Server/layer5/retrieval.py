"""
"RAG" here means retrieving relevant STRUCTURED rows from Postgres, not
vector-embedding-based document retrieval - there are no unstructured
documents in this system to embed. joint_risk_score and derived_metrics
are already exactly the structured facts an LLM needs; the retrieval
problem is just "which rows are relevant to this question", answered
with plain SQL filters, not a vector DB.

Two retrieval modes:
  - build_audit_context(): pulls EVERY joint's current risk state for a
    building - used by the scheduled/event-triggered audit, which by
    definition needs the full picture.
  - build_chat_context(): a lightweight heuristic extraction of which
    joint(s) a free-text question is actually about (by matching
    breaker_id/panel_id substrings mentioned in the question), falling
    back to "every currently non-healthy joint in the building" if the
    question doesn't name a specific one. This is deliberately simple
    pattern matching, not NLP/embeddings - accurate enough because
    breaker IDs are short, distinctive tokens (e.g. "brk-04"), and
    correctly returning "no idea what joint you mean, here's everything
    currently at-risk instead" is a perfectly reasonable fallback for a
    facility-manager-facing tool.
"""
import re
from dataclasses import dataclass, field


@dataclass
class RetrievedContext:
    joints: list[dict] = field(default_factory=list)
    matched_by_id: bool = False  # True if the question named a specific joint we found data for


async def build_audit_context(pool, building_id: str) -> RetrievedContext:
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (rs.panel_id, rs.breaker_id)
            rs.panel_id, rs.breaker_id, rs.severity_band, rs.cusum_value,
            rs.dr_dt_ohms_per_day, rs.projected_days_to_critical, rs.status, rs.ts,
            d.device_id
        FROM joint_risk_score rs
        JOIN devices d ON d.panel_id = rs.panel_id
        WHERE d.building_id = $1
        ORDER BY rs.panel_id, rs.breaker_id, rs.ts DESC
        """,
        building_id,
    )
    return RetrievedContext(joints=[dict(r) for r in rows])


async def build_chat_context(pool, building_id: str, question: str) -> RetrievedContext:
    # Look for tokens matching typical breaker_id / panel_id naming
    # (letters/digits/hyphens, 3+ chars) anywhere in the question, then
    # check which of those actually exist as real joints in this
    # building - avoids false-matching an ordinary English word.
    candidate_tokens = re.findall(r"[a-zA-Z0-9][a-zA-Z0-9\-]{2,}", question)

    matched_rows = []
    if candidate_tokens:
        rows = await pool.fetch(
            """
            SELECT DISTINCT ON (rs.panel_id, rs.breaker_id)
                rs.panel_id, rs.breaker_id, rs.severity_band, rs.cusum_value,
                rs.dr_dt_ohms_per_day, rs.projected_days_to_critical, rs.status, rs.ts,
                d.device_id
            FROM joint_risk_score rs
            JOIN devices d ON d.panel_id = rs.panel_id
            WHERE d.building_id = $1 AND (rs.breaker_id = ANY($2::text[]) OR rs.panel_id = ANY($2::text[]))
            ORDER BY rs.panel_id, rs.breaker_id, rs.ts DESC
            """,
            building_id, candidate_tokens,
        )
        matched_rows = [dict(r) for r in rows]

    if matched_rows:
        return RetrievedContext(joints=matched_rows, matched_by_id=True)

    # Fallback: no specific joint named/found - give the model every
    # currently non-healthy joint, since that's the most likely useful
    # context for an unscoped question ("what's wrong right now?").
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (rs.panel_id, rs.breaker_id)
            rs.panel_id, rs.breaker_id, rs.severity_band, rs.cusum_value,
            rs.dr_dt_ohms_per_day, rs.projected_days_to_critical, rs.status, rs.ts,
            d.device_id
        FROM joint_risk_score rs
        JOIN devices d ON d.panel_id = rs.panel_id
        WHERE d.building_id = $1 AND rs.severity_band != 'healthy'
        ORDER BY rs.panel_id, rs.breaker_id, rs.ts DESC
        """,
        building_id,
    )
    return RetrievedContext(joints=[dict(r) for r in rows], matched_by_id=False)


def context_to_prompt_json(context: RetrievedContext) -> str:
    """
    Renders retrieved rows as a compact JSON block for the prompt - the
    ONLY sensor/risk data the LLM ever sees, per the "structured trend
    data only, never raw sensor readings" requirement. Returns an
    explicit empty-array marker (not an empty string) when there's
    nothing - this is what app/layer5/grounding_check.py and the prompt
    template both rely on to trigger the "no data available" guardrail
    path instead of letting the model improvise.
    """
    import json
    return json.dumps([
        {
            "panel_id": j["panel_id"], "breaker_id": j["breaker_id"],
            "severity": j["severity_band"], "cusum": round(j["cusum_value"], 2) if j["cusum_value"] is not None else None,
            "resistance_drift_ohms_per_day": j["dr_dt_ohms_per_day"],
            "projected_days_to_critical": j["projected_days_to_critical"],
            "last_updated": j["ts"].isoformat() if j["ts"] else None,
        }
        for j in context.joints
    ], indent=2)
