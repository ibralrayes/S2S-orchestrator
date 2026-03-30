from __future__ import annotations

import io
import wave
from collections.abc import AsyncIterator

import httpx
from livekit import rtc

from config import TTSSettings


class CustomTTSAdapter:
    """Simple HTTP adapter for external speech synthesis endpoints."""

    def __init__(self, settings: TTSSettings) -> None:
        self.settings = settings
        self._client = httpx.AsyncClient(timeout=settings.timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def synthesize(self, text: str) -> AsyncIterator[rtc.AudioFrame]:
        if not text.strip():
            return

        response = await self._client.post(
            self.settings.url,
            json={
                "model": self.settings.model,
                "voice": self.settings.voice,
                "input": text,
                "response_format": self.settings.audio_format,
            },
        )
        response.raise_for_status()

        audio_bytes = response.content
        sample_rate = self.settings.sample_rate
        num_channels = self.settings.num_channels

        if self.settings.audio_format.lower() == "wav" or audio_bytes[:4] == b"RIFF":
            sample_rate, num_channels, audio_bytes = _decode_wav(audio_bytes)

        frame_bytes = _frame_size_bytes(sample_rate, num_channels)
        for offset in range(0, len(audio_bytes), frame_bytes):
            chunk = audio_bytes[offset : offset + frame_bytes]
            if len(chunk) < frame_bytes:
                chunk = chunk + (b"\x00" * (frame_bytes - len(chunk)))
            yield rtc.AudioFrame(
                data=bytearray(chunk),
                sample_rate=sample_rate,
                num_channels=num_channels,
                samples_per_channel=sample_rate // 100,
            )


def _decode_wav(wav_bytes: bytes) -> tuple[int, int, bytes]:
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav_file:
        sample_rate = wav_file.getframerate()
        num_channels = wav_file.getnchannels()
        pcm = wav_file.readframes(wav_file.getnframes())
    return sample_rate, num_channels, pcm


def _frame_size_bytes(sample_rate: int, num_channels: int) -> int:
    samples_per_channel = sample_rate // 100
    return samples_per_channel * num_channels * 2

