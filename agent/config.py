from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class STTSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOM_STT_", extra="ignore")

    url: str = Field(..., description="External transcription endpoint")
    model: str = Field(..., description="ASR model name")
    language: str = Field(default="ar", description="Language hint")
    timeout_seconds: float = Field(default=30.0, ge=1)
    target_sample_rate: int = Field(default=16000, ge=8000)


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOM_LLM_", extra="ignore")

    url: str = Field(..., description="External LLM base URL or chat endpoint")
    provider: str = Field(default="openai", description="openai or nusuk")
    model: str = Field(default="placeholder", description="LLM model name when the provider uses one")
    access_token: str | None = Field(default=None, description="Bearer token for the LLM API")
    language: str = Field(default="ar", description="Language hint for the LLM service")
    include_metadata: bool = Field(default=True, description="Request metadata when the provider supports it")
    tool: str = Field(default="Knowledge", description="Nusuk tool name")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=256, ge=1)
    timeout_seconds: float = Field(default=60.0, ge=1)


class TTSSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOM_TTS_", extra="ignore")

    url: str = Field(..., description="External TTS endpoint")
    model: str = Field(..., description="TTS model name")
    voice: str = Field(default="default", description="Requested voice")
    sample_rate: int = Field(default=24000, ge=8000)
    num_channels: int = Field(default=1, ge=1)
    audio_format: str = Field(default="wav", description="wav or pcm")
    timeout_seconds: float = Field(default=60.0, ge=1)


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore")

    name: str = Field(default="nusuk-agent")
    system_prompt: str = Field(
        default="You are a concise, helpful Arabic-first voice assistant."
    )
    greeting: str = Field(
        default="مرحبا، أنا مساعدك الصوتي. كيف أقدر أساعدك؟"
    )
    use_turn_detector: bool = Field(default=False)
    vad_activation_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    allow_interruptions: bool = Field(default=True)
    discard_audio_if_uninterruptible: bool = Field(default=True)
    min_interruption_duration: float = Field(default=0.5, ge=0.0)
    min_interruption_words: int = Field(default=0, ge=0)
    min_endpointing_delay: float = Field(default=0.5, ge=0.0)
    max_endpointing_delay: float = Field(default=5.0, ge=0.0)
    false_interruption_timeout: float | None = Field(default=2.0, ge=0.0)
    resume_false_interruption: bool = Field(default=True)
    min_consecutive_speech_delay: float = Field(default=0.0, ge=0.0)
    use_tts_aligned_transcript: bool = Field(default=False)
    participant_identity: str | None = Field(default=None)
    close_on_disconnect: bool = Field(default=True)
    delete_room_on_close: bool = Field(default=False)
