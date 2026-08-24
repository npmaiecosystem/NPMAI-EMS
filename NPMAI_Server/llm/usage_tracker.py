"""
Persists every LLM call's cost to llm_usage (migrations/
006_layer5_llm_usage_and_config.sql) and enforces a per-building daily
spend cap BEFORE making a call - our own replacement for LiteLLM's
built-in spend tracking, per the explicit "do not use litellm, develop
our own logic" requirement.
"""
import logging
from datetime import date

import asyncpg

from app.config import settings
from app.llm.base import LLMResponse
from app.llm.pricing import cost_usd

logger = logging.getLogger(__name__)


class SpendCapExceeded(Exception):
    """Raised by check_spend_cap() before any provider is called - this
    must be checked BEFORE spending money, not after, which is why it's
    a separate function from record_usage() rather than folded into it."""


async def check_spend_cap(pool: asyncpg.Pool, building_id: str) -> None:
    """
    Raises SpendCapExceeded if this building has already spent
    settings.layer5_daily_spend_cap_usd or more today (UTC calendar
    day). Callers (audit_service.py, chat_service.py) must call this
    BEFORE app.llm.registry.generate_with_failover() - checking after
    the fact only tells you that you already overspent.
    """
    if settings.layer5_daily_spend_cap_usd is None:
        return  # no cap configured - unlimited spend, an explicit opt-out, not a default

    row = await pool.fetchrow(
        """
        SELECT COALESCE(SUM(cost_usd), 0) AS total
        FROM llm_usage
        WHERE building_id = $1 AND ts::date = CURRENT_DATE
        """,
        building_id,
    )
    spent_today = float(row["total"])
    if spent_today >= settings.layer5_daily_spend_cap_usd:
        raise SpendCapExceeded(
            f"building '{building_id}' has spent ${spent_today:.4f} today, "
            f"at/over the ${settings.layer5_daily_spend_cap_usd:.4f} daily cap"
        )


async def record_usage(pool: asyncpg.Pool, building_id: str, purpose: str, response: LLMResponse) -> None:
    """
    purpose: a short free-text tag ('scheduled_audit', 'event_alert',
    'chat_console') so a later spend breakdown can answer "what's
    actually costing money", not just "how much did we spend total".
    Never raises - a failure to LOG a cost must not be treated as a
    reason to fail the request that already succeeded and already cost
    real money; it's logged loudly instead.
    """
    cost = cost_usd(response.provider, response.model, response.input_tokens, response.output_tokens)
    try:
        await pool.execute(
            """
            INSERT INTO llm_usage (building_id, purpose, provider, model, input_tokens, output_tokens, cost_usd)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            building_id, purpose, response.provider, response.model,
            response.input_tokens, response.output_tokens, cost,
        )
    except Exception:
        logger.exception(
            "Failed to record LLM usage for building=%s purpose=%s provider=%s - "
            "the call already happened and already cost money; this only means "
            "it's missing from spend tracking, investigate DB connectivity",
            building_id, purpose, response.provider,
        )
