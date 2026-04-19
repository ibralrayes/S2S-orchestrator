from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncGenerator

import httpx
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, NOT_GIVEN, llm

from config import AgentSettings, LLMSettings
from plugins.nusuk_auth import NusukAuthError, NusukTokenManager

logger = logging.getLogger("nusuk-agent.llm")


class CustomLLM(llm.LLM):
    """LiveKit-native LLM provider backed by Groq/OpenAI-style or Nusuk APIs."""

    def __init__(
        self,
        settings: LLMSettings,
        agent_settings: AgentSettings,
        *,
        session_id: str,
        user_id: str | None = None,
    ) -> None:
        super().__init__()
        self.settings = settings
        self.agent_settings = agent_settings
        self.session_id = session_id
        self.user_id = user_id
        self._provider_key = settings.provider.strip().lower()
        self._client = httpx.AsyncClient(timeout=settings.timeout_seconds)

        self.token_manager: NusukTokenManager | None = None
        if self._provider_key == "nusuk" and settings.client_id and settings.client_secret:
            self.token_manager = NusukTokenManager(
                base_url=settings.url,
                client_id=settings.client_id,
                client_secret=settings.client_secret,
                client=self._client,
            )

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def provider(self) -> str:
        return self.settings.provider

    def chat(
        self,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
        parallel_tool_calls=NOT_GIVEN,
        tool_choice=NOT_GIVEN,
        extra_kwargs=NOT_GIVEN,
    ) -> llm.LLMStream:
        del parallel_tool_calls, tool_choice, extra_kwargs
        return CustomLLMStream(
            self,
            chat_ctx=chat_ctx,
            tools=tools or [],
            conn_options=conn_options,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class CustomLLMStream(llm.LLMStream):
    def __init__(
        self,
        llm_provider: CustomLLM,
        *,
        chat_ctx: llm.ChatContext,
        tools: list[llm.Tool],
        conn_options: APIConnectOptions,
    ) -> None:
        super().__init__(
            llm=llm_provider,
            chat_ctx=chat_ctx,
            tools=tools,
            conn_options=conn_options,
        )
        self._provider = llm_provider

    async def _run(self) -> None:
        if self._provider._provider_key == "nusuk":
            await self._run_nusuk()
            return
        await self._run_openai()

    async def _run_openai(self) -> None:
        messages, _ = self.chat_ctx.to_provider_format("openai")
        if not any(message.get("role") == "system" for message in messages):
            messages.insert(
                0,
                {
                    "role": "system",
                    "content": self._provider.agent_settings.system_prompt,
                },
            )

        payload = {
            "model": self._provider.settings.model,
            "messages": messages,
            "stream": True,
            "reasoning_effort": "none",
            "temperature": self._provider.settings.temperature,
            "max_tokens": self._provider.settings.max_tokens,
        }

        request_id = str(uuid.uuid4())
        reasoning_filter = ReasoningStreamFilter()

        logger.info("llm_start provider=%s", self._provider.settings.provider)
        async with self._provider._client.stream(
            "POST",
            _openai_chat_url(self._provider.settings.url),
            json=payload,
            headers=_bearer_headers(self._provider.settings),
        ) as response:
            response.raise_for_status()
            async for event in _iter_sse(response):
                request_id = event.get("id") or request_id
                delta = _extract_openai_delta(event)
                if not delta:
                    continue
                filtered = reasoning_filter.push(delta)
                if not filtered:
                    continue
                self._event_ch.send_nowait(
                    llm.ChatChunk(
                        id=request_id,
                        delta=llm.ChoiceDelta(role="assistant", content=filtered),
                    )
                )
        logger.info("llm_done provider=%s", self._provider.settings.provider)

    async def _run_nusuk(self) -> None:
        query = _latest_user_message(self.chat_ctx)
        if not query:
            return

        prefix = self._provider.settings.query_prefix
        if prefix:
            query = f"{prefix.strip()} {query}"

        payload = {
            "query": query,
            "session_id": self._provider.session_id,
            "language": self._provider.settings.language,
            "include_metadata": self._provider.settings.include_metadata,
            "tool": self._provider.settings.tool,
        }
        if self._provider.user_id:
            payload["user_id"] = self._provider.user_id

        request_id = str(uuid.uuid4())
        logger.info(
            "llm_start provider=%s session_id=%s query_len=%d",
            self._provider.settings.provider,
            self._provider.session_id,
            len(query),
        )

        for attempt in range(2):
            headers = await self._nusuk_headers()
            try:
                async with self._provider._client.stream(
                    "POST",
                    _nusuk_stream_url(self._provider.settings.url),
                    json=payload,
                    headers=headers,
                ) as response:
                    if response.status_code == 401 and attempt == 0 and self._provider.token_manager:
                        await response.aread()
                        logger.warning("llm_nusuk_401_invalidating_token")
                        await self._provider.token_manager.invalidate()
                        continue
                    response.raise_for_status()
                    async for event in _iter_sse(response):
                        delta = event.get("delta")
                        if not isinstance(delta, str) or not delta:
                            continue
                        self._event_ch.send_nowait(
                            llm.ChatChunk(
                                id=request_id,
                                delta=llm.ChoiceDelta(role="assistant", content=delta),
                            )
                        )
                break
            except NusukAuthError:
                logger.exception("llm_nusuk_auth_failed")
                raise
        logger.info("llm_done provider=%s", self._provider.settings.provider)

    async def _nusuk_headers(self) -> dict[str, str]:
        if self._provider.token_manager is not None:
            token = await self._provider.token_manager.get_token()
            return {"Authorization": f"Bearer {token}"}
        return _bearer_headers(self._provider.settings)


async def _iter_sse(response: httpx.Response) -> AsyncGenerator[dict, None]:
    """Yield parsed JSON events from an SSE response, skipping non-data lines and [DONE]."""
    async for line in response.aiter_lines():
        if not line or not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if data == "[DONE]":
            return
        try:
            yield json.loads(data)
        except json.JSONDecodeError:
            logger.warning("sse_bad_chunk data=%s", data[:120])


def _extract_openai_delta(event: dict) -> str | None:
    """Extract the text delta from an OpenAI-style SSE chunk."""
    return event.get("choices", [{}])[0].get("delta", {}).get("content")


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


class ReasoningStreamFilter:
    def __init__(self) -> None:
        self._raw = ""
        self._visible_len = 0

    def push(self, chunk: str) -> str:
        self._raw += chunk
        visible = _visible_text(self._raw)
        if len(visible) <= self._visible_len:
            return ""
        delta = visible[self._visible_len :]
        self._visible_len = len(visible)
        return delta


def _visible_text(text: str) -> str:
    cleaned = text
    while True:
        start = cleaned.find("<think>")
        if start == -1:
            break
        end = cleaned.find("</think>", start + len("<think>"))
        if end == -1:
            cleaned = cleaned[:start]
            break
        cleaned = cleaned[:start] + cleaned[end + len("</think>") :]

    for suffix in ("<think>", "<think", "<thin", "<thi", "<th", "<t", "<"):
        if cleaned.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break

    return cleaned
