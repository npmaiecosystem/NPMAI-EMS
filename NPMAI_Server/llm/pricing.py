"""
Our own cost tracking, since the whole point of not using LiteLLM is
that we don't inherit its dependency, its spend-tracking assumptions, or
any cost of running/maintaining it. This is genuinely simple: a price
table (USD per million tokens, input/output priced separately since
every provider charges them differently) and one multiplication.

PRICES BELOW WILL GO STALE - every provider changes pricing periodically
and this file has no way to auto-detect that. Review and update
PRICING_USD_PER_MILLION_TOKENS against each provider's current pricing
page before relying on cost figures for real budgeting decisions;
treat the numbers here as "known correct as of when this file was last
edited", not as continuously accurate. This is an intentional trade-off
of not depending on a third-party library that might track this for
you - you own updating it, in exchange for owning everything else about
this integration too.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# (provider, model) -> (input_usd_per_million_tokens, output_usd_per_million_tokens)
# Fill in with each provider's current published pricing before going live.
PRICING_USD_PER_MILLION_TOKENS: dict[tuple[str, str], tuple[float, float]] = {
    # Placeholder structure - REPLACE with real current prices per your
    # chosen models before relying on cost figures operationally.
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("anthropic", "claude-sonnet-5"): (3.00, 15.00),
    ("gemini", "gemini-1.5-pro"): (1.25, 5.00),
    ("grok", "grok-4"): (3.00, 15.00),
}


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> Optional[float]:
    """
    Returns None (not 0.0) when a (provider, model) pair isn't in the
    pricing table - None means "unknown cost", which is a meaningfully
    different fact from "this call was free", and callers
    (app/llm/usage_tracker.py) must not silently treat unknown cost as
    zero cost when checking spend caps.
    """
    prices = PRICING_USD_PER_MILLION_TOKENS.get((provider, model))
    if prices is None:
        logger.warning(
            "No pricing entry for provider=%s model=%s - cost cannot be calculated for this call. "
            "Add an entry to PRICING_USD_PER_MILLION_TOKENS in app/llm/pricing.py.",
            provider, model,
        )
        return None
    input_price, output_price = prices
    return (input_tokens / 1_000_000.0) * input_price + (output_tokens / 1_000_000.0) * output_price
