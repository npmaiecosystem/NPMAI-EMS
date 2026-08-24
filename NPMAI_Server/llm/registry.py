"""
Builds LLMProvider instances from config and tries them in order,
falling to the next on any LLMError - this IS the "not just ChatGPT/
Gemini/Claude, also Grok, and don't limit us" requirement made concrete:
adding a new provider means adding one PROVIDER_CONFIGS entry, never
touching this file's logic or any caller's code.

Config-driven, not hardcoded, so the primary provider and its fallback
order can be changed via environment variables without a code change or
redeploy - see app/config.py's layer5_primary_provider /
layer5_fallback_providers fields.
"""
import logging
from typing import Optional

from app.config import settings
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMError, LLMProvider, LLMResponse
from app.llm.gemini_provider import GeminiProvider
from app.llm.openai_compatible import OpenAICompatibleProvider

logger = logging.getLogger(__name__)

# Every entry here is (provider_class, kwargs). Adding a brand-new
# OpenAI-compatible provider (Mistral, DeepSeek, Groq, Together, a local
# Ollama server, anything) is a THREE-LINE addition to this dict -
# nothing else in the codebase needs to know it exists.
PROVIDER_CONFIGS = {
    "openai": lambda: OpenAICompatibleProvider(
        provider_name="openai", base_url="https://api.openai.com/v1",
        api_key=settings.openai_api_key, model=settings.openai_model,
    ),
    "grok": lambda: OpenAICompatibleProvider(
        provider_name="grok", base_url="https://api.x.ai/v1",
        api_key=settings.grok_api_key, model=settings.grok_model,
    ),
    "anthropic": lambda: AnthropicProvider(
        api_key=settings.anthropic_api_key, model=settings.anthropic_model,
    ),
    "gemini": lambda: GeminiProvider(
        api_key=settings.gemini_api_key, model=settings.gemini_model,
    ),
    # Example of a THIRD OpenAI-compatible provider added for free, to
    # make the "don't limit us" point concrete rather than theoretical -
    # delete if unused, or add your own real Groq/Mistral/DeepSeek/etc.
    # key the same way:
    # "groq": lambda: OpenAICompatibleProvider(
    #     provider_name="groq", base_url="https://api.groq.com/openai/v1",
    #     api_key=settings.groq_api_key, model=settings.groq_model,
    # ),
}


def build_provider(name: str) -> Optional[LLMProvider]:
    factory = PROVIDER_CONFIGS.get(name)
    if factory is None:
        logger.error("Unknown provider name '%s' - check PROVIDER_CONFIGS and your config's provider name fields", name)
        return None
    try:
        return factory()
    except Exception:
        logger.exception("Failed to construct provider '%s' (likely a missing/invalid API key in config)", name)
        return None


async def generate_with_failover(system_prompt: str, user_prompt: str, max_output_tokens: int) -> LLMResponse:
    """
    Tries settings.layer5_primary_provider first, then each of
    settings.layer5_fallback_providers in order, returning the first
    successful response. Raises LLMError only if EVERY configured
    provider fails - callers (audit_service.py, chat_service.py) treat
    that as "no LLM available right now", not a per-provider concern.
    """
    provider_order = [settings.layer5_primary_provider] + list(settings.layer5_fallback_providers)
    last_error: Optional[Exception] = None

    for name in provider_order:
        provider = build_provider(name)
        if provider is None:
            continue
        try:
            return await provider.generate(system_prompt, user_prompt, max_output_tokens)
        except LLMError as exc:
            logger.warning("Provider '%s' failed: %s - trying next provider in fallback order", name, exc)
            last_error = exc
            continue

    raise LLMError("all_providers", f"every configured provider failed; last error: {last_error}")
