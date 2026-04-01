from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from livekit.agents import llm

from config import AgentSettings, LLMSettings


class CustomLLMAdapter:
    """Streaming chat adapter for OpenAI-style and Nusuk APIs."""

    def __init__(
        self,
        settings: LLMSettings,
        agent_settings: AgentSettings,
        *,
        session_id: str,
        user_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.agent_settings = agent_settings
        self.session_id = session_id
        self.user_id = user_id
        self._client = httpx.AsyncClient(timeout=settings.timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def stream_chat(self, chat_ctx: llm.ChatContext) -> AsyncIterator[str]:
        provider = self.settings.provider.strip().lower()
        if provider == "nusuk":
            async for chunk in self._stream_nusuk(chat_ctx):
                yield chunk
            return

        async for chunk in self._stream_openai(chat_ctx):
            yield chunk

    async def _stream_openai(self, chat_ctx: llm.ChatContext) -> AsyncIterator[str]:
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
            "POST",
            _openai_chat_url(self.settings.url),
            json=payload,
            headers=_bearer_headers(self.settings),
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

    async def _stream_nusuk(self, chat_ctx: llm.ChatContext) -> AsyncIterator[str]:
        query = _latest_user_message(chat_ctx)
        if not query:
            return

        payload = {
            "query": query,
            "session_id": self.session_id,
            "language": self.settings.language,
            "include_metadata": self.settings.include_metadata,
            "tool": self.settings.tool,
        }
        if self.user_id:
            payload["user_id"] = self.user_id

        async with self._client.stream(
            "POST",
            _nusuk_stream_url(self.settings.url),
            json=payload,
            headers=_bearer_headers(self.settings),
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

                delta = event.get("delta")
                if isinstance(delta, str) and delta:
                    yield delta


def _openai_chat_url(url: str) -> str:
    if url.rstrip("/").endswith("/chat/completions"):
        return url
    return url.rstrip("/") + "/chat/completions"


def _nusuk_stream_url(url: str) -> str:
    normalized = url.rstrip("/")
    if normalized.endswith("/chat/stream"):
        return normalized
    if normalized.endswith("/chat"):
        return normalized + "/stream"
    return normalized + "/chat/stream"


def _bearer_headers(settings: LLMSettings) -> dict[str, str]:
    if not settings.access_token:
        return {}
    return {"Authorization": f"Bearer {settings.access_token}"}


def _latest_user_message(chat_ctx: llm.ChatContext) -> str:
    messages, _ = chat_ctx.to_provider_format("openai")
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        text = _message_text(message.get("content"))
        if text:
            return text
    return ""


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                text = item["text"].strip()
                if text:
                    parts.append(text)
        return " ".join(parts)
    return ""
