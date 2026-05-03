from __future__ import annotations

import io
import logging
import time
import uuid
import wave
from dataclasses import dataclass
from typing import Any

import httpx
from livekit import rtc
from livekit.agents import stt, utils

import metrics
from config import STTSettings

logger = logging.getLogger("nusuk-agent.stt")


@dataclass(slots=True)
class STTResult:
    text: str
    request_id: str
    language: str


class CustomSTTAdapter(stt.STT):
    """HTTP adapter for local ASR, OpenAI-style, and Nusuk transcription endpoints."""

    def __init__(self, settings: STTSettings) -> None:
        super().__init__(
            capabilities=stt.STTCapabilities(
                streaming=False,
                interim_results=False,
                diarization=False,
            )
        )
        self.settings = settings
        self._provider_key = settings.provider.strip().lower()
        self._client = httpx.AsyncClient(timeout=settings.timeout_seconds)

    @property
    def model(self) -> str:
        return self.settings.model

    @property
    def provider(self) -> str:
        return self.settings.provider

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _recognize_impl(
        self,
        buffer: utils.AudioBuffer,
        *,
        language: Any = None,
        conn_options: Any = None,  # noqa: ARG002 — required by base class signature
    ) -> stt.SpeechEvent:
        frames = buffer if isinstance(buffer, list) else [buffer]
        result = await self.transcribe_frames(frames)
        transcript_language = language if isinstance(language, str) and language else result.language
        speech_data = stt.SpeechData(language=transcript_language, text=result.text)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=result.request_id,
            alternatives=[speech_data],
        )

    async def transcribe_frames(self, frames: list[rtc.AudioFrame]) -> STTResult:
        request_id = str(uuid.uuid4())
        logger.info("request_id=%s stt_start frames=%s", request_id, len(frames))
        try:
            wav_bytes = frames_to_wav_bytes(
                frames, target_sample_rate=self.settings.target_sample_rate
            )
        except ValueError as exc:
            logger.warning("request_id=%s stt_no_audio error=%s", request_id, exc)
            return STTResult(text="", request_id=request_id, language=self.settings.language)

        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data = _request_form_data(self.settings, self._provider_key)
        t0 = time.monotonic()
        try:
            response = await self._client.post(
                _transcribe_url(self.settings.url, self._provider_key),
                data=data,
                files=files,
                headers=_bearer_headers(self.settings),
            )
            response.raise_for_status()
            metrics.STT_DURATION.observe(time.monotonic() - t0)
        except httpx.HTTPError as exc:
            metrics.STT_ERRORS.inc()
            logger.error(
                "request_id=%s stt_failed url=%s error=%s",
                request_id,
                _transcribe_url(self.settings.url, self._provider_key),
                exc,
            )
            return STTResult(text="", request_id=request_id, language=self.settings.language)

        try:
            payload = response.json()
        except ValueError as exc:
            logger.error("request_id=%s stt_bad_json error=%s", request_id, exc)
            return STTResult(text="", request_id=request_id, language=self.settings.language)

        text = ""
        for key in ("transcription_text", "text", "transcript", "transcription"):
            v = payload.get(key)
            if isinstance(v, str) and v:
                text = v
                break
        resolved_request_id = _response_request_id(payload, request_id)
        logger.info(
            "request_id=%s stt_done provider=%s text=%s",
            request_id,
            self.settings.provider,
            text.strip(),
        )
        return STTResult(
            text=text.strip(),
            request_id=resolved_request_id,
            language=payload.get("language", self.settings.language),
        )


def _request_form_data(settings: STTSettings, provider: str) -> dict[str, str]:
    if provider in {"nusuk", "local_api"}:
        return {}
    return {
        "model": settings.model,
        "language": settings.language,
    }


def _transcribe_url(url: str, provider: str) -> str:
    normalized = url.rstrip("/")
    if provider == "local_api" and not normalized.endswith("/api/transcribe"):
        return normalized + "/api/transcribe/"
    if provider == "nusuk" and not normalized.endswith("/transcribe"):
        return normalized + "/transcribe"
    return normalized


def _bearer_headers(settings: STTSettings) -> dict[str, str]:
    if not settings.access_token:
        return {}
    return {"Authorization": f"Bearer {settings.access_token}"}


def _response_request_id(payload: dict[str, object], fallback: str) -> str:
    transcription_id = payload.get("transcription_id")
    if transcription_id is not None:
        return str(transcription_id)
    request_id = payload.get("request_id")
    if isinstance(request_id, str) and request_id:
        return request_id
    return fallback


def frames_to_wav_bytes(
    frames: list[rtc.AudioFrame], *, target_sample_rate: int
) -> bytes:
    if not frames:
        raise ValueError("No audio frames available for transcription")

    merged = rtc.combine_audio_frames(frames)
    pcm_bytes = bytes(merged.data.cast("b"))
    sample_rate = merged.sample_rate

    if sample_rate != target_sample_rate:
        resampler = rtc.AudioResampler(
            sample_rate,
            target_sample_rate,
            num_channels=merged.num_channels,
            quality=rtc.AudioResamplerQuality.HIGH,
        )
        resampled_frames = resampler.push(bytearray(pcm_bytes)) + resampler.flush()
        merged = rtc.combine_audio_frames(resampled_frames)
        pcm_bytes = bytes(merged.data.cast("b"))
        sample_rate = merged.sample_rate

    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as wav_file:
        wav_file.setnchannels(merged.num_channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return wav_buffer.getvalue()
