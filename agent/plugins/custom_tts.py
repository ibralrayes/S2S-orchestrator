from __future__ import annotations

import io
import logging
import time
import uuid
import wave

import httpx
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, tts
from livekit.agents.tts.tts import AudioEmitter

import metrics
import observability
from config import TTSSettings

logger = logging.getLogger("nusuk-agent.tts")


class CustomTTS(tts.TTS):
    """LiveKit-native non-streaming TTS provider backed by the local HTTP API."""

    def __init__(self, settings: TTSSettings) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=settings.sample_rate,
            num_channels=settings.num_channels,
        )
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=settings.timeout_seconds)

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def provider(self) -> str:
        return self.settings.provider

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return CustomTTSChunkedStream(
            tts_provider=self,
            input_text=text,
            conn_options=conn_options,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class CustomTTSChunkedStream(tts.ChunkedStream):
    def __init__(
        self,
        *,
        tts_provider: CustomTTS,
        input_text: str,
        conn_options: APIConnectOptions,
    ) -> None:
        self._provider = tts_provider
        super().__init__(
            tts=tts_provider,
            input_text=input_text,
            conn_options=conn_options,
        )

    async def _run(self, output_emitter: AudioEmitter) -> None:
        text = _strip_markdown(self.input_text).strip()
        if not text:
            return

        span = observability.start_span(
            name="tts",
            input={
                "provider": self._provider.settings.provider,
                "text": text,
                "text_len": len(text),
            },
        )

        logger.info(
            "tts_start provider=%s text_len=%d",
            self._provider.settings.provider,
            len(text),
        )
        t0 = time.monotonic()
        try:
            response = await self._provider._client.post(
                _tts_url(self._provider.settings.url, self._provider.settings.provider),
                json=_request_payload(
                    self._provider.settings,
                    text,
                    self._provider.settings.provider,
                ),
                headers=_bearer_headers(self._provider.settings),
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            metrics.TTS_ERRORS.inc()
            logger.error("tts_failed error=%s url=%s", exc, getattr(exc, "request", None))
            span.update(level="ERROR", status_message=str(exc))
            span.end()
            request_id = str(uuid.uuid4())
            output_emitter.initialize(
                request_id=request_id,
                sample_rate=self._provider.settings.sample_rate,
                num_channels=self._provider.settings.num_channels,
                mime_type="audio/pcm",
                frame_size_ms=20,
            )
            return

        audio_bytes = response.content
        sample_rate = self._provider.settings.sample_rate
        num_channels = self._provider.settings.num_channels

        if audio_bytes[:4] == b"RIFF":
            sample_rate, num_channels, audio_bytes = _decode_wav(audio_bytes)

        request_id = response.headers.get("x-synthesis-id") or str(uuid.uuid4())
        output_emitter.initialize(
            request_id=str(request_id),
            sample_rate=sample_rate,
            num_channels=num_channels,
            mime_type="audio/pcm",
            frame_size_ms=20,
        )
        output_emitter.push(audio_bytes)
        duration_s = time.monotonic() - t0
        metrics.TTS_DURATION.observe(duration_s)
        span.update(
            output={
                "request_id": str(request_id),
                "audio_bytes": len(audio_bytes),
                "sample_rate": sample_rate,
            },
            metadata={"duration_s": duration_s},
        )
        span.end()
        logger.info(
            "tts_done request_id=%s audio_bytes=%d sample_rate=%d duration_s=%.3f",
            request_id,
            len(audio_bytes),
            sample_rate,
            duration_s,
        )


def _request_payload(settings: TTSSettings, text: str, provider: str) -> dict[str, object]:
    if provider == "wrapper":
        return {"text": text}
    if provider == "local_api":
        return {
            "text": text,
            "output_format": settings.audio_format,
            "sample_rate": settings.sample_rate,
        }
    return {
        "model": settings.model,
        "voice": settings.voice,
        "input": text,
        "response_format": settings.audio_format,
    }


def _tts_url(url: str, provider: str) -> str:
    normalized = url.rstrip("/")
    if provider == "wrapper":
        return normalized
    if provider == "local_api":
        base = normalized.removesuffix("/api/synthesize/").removesuffix("/api/synthesize")
        return base + "/api/synthesize/"
    return normalized


def _bearer_headers(settings: TTSSettings) -> dict[str, str]:
    if not settings.access_token:
        return {}
    return {"Authorization": f"Bearer {settings.access_token}"}


def _strip_markdown(text: str) -> str:
    """Remove markdown formatting that TTS would speak literally."""
    import re
    text = re.sub(r'\*+([^*\n]+)\*+', r'\1', text)   # **bold** / *italic*
    text = re.sub(r'^\s*>+\s*', '', text, flags=re.MULTILINE)  # > blockquotes
    text = re.sub(r'\[\d+\]', '', text)               # [4] citation markers
    text = re.sub(r'\n{2,}', ' ', text)               # collapse paragraph breaks
    return text.strip()


def _decode_wav(wav_bytes: bytes) -> tuple[int, int, bytes]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        num_channels = wav_file.getnchannels()
        pcm = wav_file.readframes(wav_file.getnframes())
    return sample_rate, num_channels, pcm
