from __future__ import annotations

import io
import uuid
import wave
from dataclasses import dataclass

import httpx
from livekit import rtc

from config import STTSettings


@dataclass(slots=True)
class STTResult:
    text: str
    request_id: str
    language: str


class CustomSTTAdapter:
    """Small HTTP adapter for Whisper/OpenAI-style transcription endpoints."""

    def __init__(self, settings: STTSettings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=settings.timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def transcribe_frames(self, frames: list[rtc.AudioFrame]) -> STTResult:
        request_id = str(uuid.uuid4())
        wav_bytes = frames_to_wav_bytes(
            frames, target_sample_rate=self.settings.target_sample_rate
        )
        files = {"file": ("audio.wav", wav_bytes, "audio/wav")}
        data = {"model": self.settings.model, "language": self.settings.language}
        response = await self._client.post(self.settings.url, data=data, files=files)
        response.raise_for_status()

        payload = response.json()
        text = (
            payload.get("text")
            or payload.get("transcript")
            or payload.get("transcription")
            or ""
        )
        return STTResult(
            text=text.strip(),
            request_id=payload.get("request_id", request_id),
            language=payload.get("language", self.settings.language),
        )


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

