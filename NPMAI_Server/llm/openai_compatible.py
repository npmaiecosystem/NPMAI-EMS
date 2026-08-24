"""
Covers any provider whose API follows OpenAI's /chat/completions request
and response shape - which today includes OpenAI itself, xAI's Grok,
Mistral, DeepSeek, Groq, Together, and most newer entrants, since this
shape has become a de facto standard. This is the single biggest lever
for the "don't limit us to ChatGPT/Gemini/Claude, we need Grok and
others too" requirement: adding a brand-new OpenAI-compatible provider
later means adding one entry to app/llm/registry.py's PROVIDER_CONFIGS
(base_url + api_key + model name), not writing a new adapter class.

Only Anthropic (different request/response shape entirely - see
anthropic_provider.py) and Google Gemini (also a distinct shape - see
gemini_provider.py) need their own dedicated adapters, because their
APIs are NOT OpenAI-compatible.
"""
import httpx

from app.llm.base import LLMError, LLMResponse


class OpenAICompatibleProvider:
    def __init__(self, provider_name: str, base_url: str, api_key: str, model: str, timeout_seconds: float = 60.0):
        self.provider_name = provider_name
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds

    async def generate(self, system_prompt: str, user_prompt: str, max_output_tokens: int) -> LLMResponse:
        url = f"{self._base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_output_tokens,
            "temperature": 0.2,  # low temperature: this system produces structured diagnostic output, not creative text - consistency across repeated audits of the same joint matters more than variety
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except httpx.RequestError as exc:
            raise LLMError(self.provider_name, f"network error calling {url}: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(self.provider_name, f"non-200 response: {resp.text[:500]}", status_code=resp.status_code)

        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            input_tokens = int(usage.get("prompt_tokens", 0))
            output_tokens = int(usage.get("completion_tokens", 0))
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise LLMError(self.provider_name, f"unexpected response shape: {exc}") from exc

        return LLMResponse(
            text=text, provider=self.provider_name, model=self.model,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
