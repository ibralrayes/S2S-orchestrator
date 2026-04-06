from __future__ import annotations

import io
import logging
import uuid
import wave

import httpx
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions, tts
from livekit.agents.tts.tts import AudioEmitter

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
        if not self.input_text.strip():
            return

        logger.info("tts_start text=%s", self.input_text)
        response = await self._provider._client.post(
            _tts_url(self._provider.settings.url, self._provider.settings.provider),
            json=_request_payload(
                self._provider.settings,
                self.input_text,
                self._provider.settings.provider,
            ),
            headers=_bearer_headers(self._provider.settings),
        )
        response.raise_for_status()

        audio_bytes = response.content
        sample_rate = self._provider.settings.sample_rate
        num_channels = self._provider.settings.num_channels

        if self._provider.settings.audio_format.lower() == "wav" or audio_bytes[:4] == b"RIFF":
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
        logger.info("tts_done request_id=%s", request_id)


def _request_payload(settings: TTSSettings, text: str, provider: str) -> dict[str, object]:
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
    if provider == "local_api":
        if normalized.endswith("/api/synthesize"):
            return normalized + "/"
        if normalized.endswith("/api/synthesize/"):
            return normalized
        return normalized + "/api/synthesize/"
    return normalized


def _bearer_headers(settings: TTSSettings) -> dict[str, str]:
    if not settings.access_token:
        return {}
    return {"Authorization": f"Bearer {settings.access_token}"}


def _decode_wav(wav_bytes: bytes) -> tuple[int, int, bytes]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        num_channels = wav_file.getnchannels()
        pcm = wav_file.readframes(wav_file.getnframes())
    return sample_rate, num_channels, pcm
