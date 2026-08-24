"""
The core abstraction every LLM provider in this system implements.
Deliberately NOT built on LangChain, LiteLLM, or any other orchestration
framework - every provider adapter in app/llm/ talks to that provider's
own official REST API directly over httpx, using that provider's own
documented request/response JSON shape. Nothing here "wraps" an SDK;
each adapter IS the integration.

Why this matters practically, not just as a policy: LiteLLM/LangChain's
value proposition is normalizing away exactly the differences between
providers' request/response shapes - which is convenient, but it also
means a provider changing their API breaks you inside someone else's
abstraction layer you don't control, and it pulls in a dependency whose
own release cadence/bugs become your problem. Talking to each REST API
directly means when Anthropic or OpenAI changes something, you're
reading THEIR changelog and fixing YOUR adapter function - a much
smaller, more legible blast radius, and zero third-party wrapper cost or
version-pinning risk (this directly answers the "ensure litellm or
whatever we use do not cost" requirement - there IS no such library
here, so there's nothing to cost).

Every adapter must return an LLMResponse with real token counts pulled
from that provider's own response payload (never estimated/guessed) -
app/llm/pricing.py's cost calculation is only as trustworthy as these
numbers being the provider's own reported usage.
"""
from dataclasses import dataclass
from typing import Optional, Protocol


class LLMError(Exception):
    """Raised by any provider adapter on a failed call - network error,
    non-2xx response, malformed response body, or a response that's
    missing the fields we need (text/usage). Callers (app/llm/registry.py)
    catch this specifically to move to the next provider in a fallback
    chain, so a single provider outage never takes down Layer 5."""

    def __init__(self, provider: str, message: str, status_code: Optional[int] = None):
        self.provider = provider
        self.status_code = status_code
        super().__init__(f"[{provider}] {message}" + (f" (HTTP {status_code})" if status_code else ""))


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int


class LLMProvider(Protocol):
    """
    Every adapter (app/llm/openai_compatible.py, anthropic_provider.py,
    gemini_provider.py) implements exactly this shape. `generate` is the
    ONLY method the rest of Layer 5 (app/layer5/audit_service.py,
    chat_service.py) ever calls - they never touch a provider-specific
    client object directly, which is what makes
    app/llm/registry.py's provider-swapping and failover possible without
    any caller-side branching on "which provider is this".
    """

    provider_name: str
    model: str

    async def generate(self, system_prompt: str, user_prompt: str, max_output_tokens: int) -> LLMResponse:
        ...
