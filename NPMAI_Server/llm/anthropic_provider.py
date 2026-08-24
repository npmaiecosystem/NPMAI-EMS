"""
Anthropic's Messages API - genuinely different from the OpenAI shape in
three ways that matter for this adapter: (1) system prompt is a
top-level `system` field, NOT a message with role="system" inside the
messages array; (2) auth header is `x-api-key`, not `Authorization:
Bearer`; (3) a required `anthropic-version` header pins the API version
explicitly. Getting any of these three wrong doesn't necessarily error
loudly - e.g. putting the system prompt in the messages array as a
role="system" entry, Claude's API rejects it outright, which is at
least loud; but this is exactly the kind of per-provider shape
difference LiteLLM/LangChain would normally paper over - here we handle
it explicitly, once, in this one file.
"""
import httpx

from app.llm.base import LLMError, LLMResponse

_ANTHROPIC_API_VERSION = "2023-06-01"


class AnthropicProvider:
    def __init__(self, api_key: str, model: str, base_url: str = "https://api.anthropic.com/v1", timeout_seconds: float = 60.0):
        self.provider_name = "anthropic"
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds

    async def generate(self, system_prompt: str, user_prompt: str, max_output_tokens: int) -> LLMResponse:
        url = f"{self._base_url}/messages"
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_API_VERSION,
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "system": system_prompt,  # top-level field, NOT inside messages - see module docstring
            "messages": [{"role": "user", "content": user_prompt}],
            "max_tokens": max_output_tokens,
            "temperature": 0.2,
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
            # content is a LIST of blocks (text, tool_use, etc.) - join
            # only the text blocks, in case a future response includes
            # other block types we're not asking for here.
            text = "".join(block["text"] for block in data["content"] if block.get("type") == "text")
            usage = data.get("usage", {})
            input_tokens = int(usage.get("input_tokens", 0))
            output_tokens = int(usage.get("output_tokens", 0))
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise LLMError(self.provider_name, f"unexpected response shape: {exc}") from exc

        return LLMResponse(
            text=text, provider=self.provider_name, model=self.model,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
