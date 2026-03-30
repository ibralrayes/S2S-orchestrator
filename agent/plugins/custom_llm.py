from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from livekit.agents import llm

from config import AgentSettings, LLMSettings


class CustomLLMAdapter:
    """OpenAI-compatible streaming chat adapter."""

    def __init__(self, settings: LLMSettings, agent_settings: AgentSettings) -> None:
        self.settings = settings
        self.agent_settings = agent_settings
        self._client = httpx.AsyncClient(timeout=settings.timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream_chat(self, chat_ctx: llm.ChatContext) -> AsyncIterator[str]:
        messages, _ = chat_ctx.to_provider_format("openai")
        if not any(message.get("role") == "system" for message in messages):
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": self.agent_settings.system_prompt,
                },
            )

        payload = {
            "model": self.settings.model,
            "messages": messages,
            "stream": True,
            "temperature": self.settings.temperature,
            "max_tokens": self.settings.max_tokens,
        }

        async with self._client.stream(
            "POST", _chat_url(self.settings.url), json=payload
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue

                delta = (
                    event.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content")
                )
                if delta:
                    yield delta


def _chat_url(url: str) -> str:
    if url.rstrip("/").endswith("/chat/completions"):
        return url
    return url.rstrip("/") + "/chat/completions"

