"""
Google Gemini's generateContent API - a third distinct shape: (1) the
API key goes in the URL query string (`?key=...`), not a header, unless
you're using Vertex AI's separate OAuth-based endpoint (not covered
here - this targets the Gemini Developer API); (2) the model name is
part of the URL PATH, not a JSON body field; (3) "messages" are called
"contents", each with a "parts" list instead of a plain "content"
string; (4) the system prompt is `systemInstruction`, a sibling of
`contents`, not a message with a role; (5) token usage comes back under
`usageMetadata` with different key names again
(promptTokenCount/candidatesTokenCount, not prompt_tokens/
completion_tokens). Every one of these is a real, separate difference
from both the OpenAI-compatible shape and Anthropic's shape - this is
the clearest illustration of why one "OpenAICompatibleProvider" class
can't stretch to cover every provider, and why Gemini needs its own
adapter here.
"""
import httpx

from app.llm.base import LLMError, LLMResponse


class GeminiProvider:
    def __init__(self, api_key: str, model: str, base_url: str = "https://generativelanguage.googleapis.com/v1beta", timeout_seconds: float = 60.0):
        self.provider_name = "gemini"
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds

    async def generate(self, system_prompt: str, user_prompt: str, max_output_tokens: int) -> LLMResponse:
        url = f"{self._base_url}/models/{self.model}:generateContent?key={self._api_key}"
        payload = {
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
            "generationConfig": {"maxOutputTokens": max_output_tokens, "temperature": 0.2},
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
        except httpx.RequestError as exc:
            raise LLMError(self.provider_name, f"network error calling {url}: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(self.provider_name, f"non-200 response: {resp.text[:500]}", status_code=resp.status_code)

        try:
            data = resp.json()
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
            usage = data.get("usageMetadata", {})
            input_tokens = int(usage.get("promptTokenCount", 0))
            output_tokens = int(usage.get("candidatesTokenCount", 0))
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise LLMError(self.provider_name, f"unexpected response shape: {exc}") from exc

        return LLMResponse(
            text=text, provider=self.provider_name, model=self.model,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
