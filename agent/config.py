from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class STTSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOM_STT_", extra="ignore")

    url: str = Field(..., description="External transcription endpoint")
    provider: str = Field(default="local_api", description="local_api, openai, or nusuk")
    model: str = Field(default="placeholder", description="ASR model name when the provider uses one")
    access_token: str | None = Field(default=None, description="Bearer token for the STT API")
    language: str = Field(default="ar", description="Language hint")
    timeout_seconds: float = Field(default=30.0, ge=1)
    target_sample_rate: int = Field(default=16000, ge=8000)


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOM_LLM_", extra="ignore")

    url: str = Field(..., description="External LLM base URL or chat endpoint")
    provider: str = Field(default="openai", description="openai or nusuk")
    model: str = Field(default="qwen/qwen3-32b", description="LLM model name when the provider uses one")
    access_token: str | None = Field(
        default=None,
        description="Bearer token for the LLM API",
        validation_alias=AliasChoices("CUSTOM_LLM_ACCESS_TOKEN", "GROQ_API_KEY", "GROQ"),
    )
    client_id: str | None = Field(
        default=None,
        description="OAuth-style client_id for providers that mint tokens on demand (e.g. Nusuk)",
    )
    client_secret: str | None = Field(
        default=None,
        description="OAuth-style client_secret paired with client_id",
    )
    auth_user_id: str | None = Field(
        default=None,
        description="user_id passed in the Nusuk /auth/token body. Defaults to client_id when unset.",
    )
    language: str = Field(default="ar", description="Language hint for the LLM service")
    query_prefix: str | None = Field(
        default=None,
        description="Text prepended to every user query (e.g. response-style instructions for providers that ignore system prompts)",
    )
    include_metadata: bool = Field(default=True, description="Request metadata when the provider supports it")
    tool: str = Field(default="Knowledge", description="Nusuk tool name")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=96, ge=1)
    reasoning_effort: str | None = Field(
        default=None,
        description="Groq gpt-oss reasoning_effort: low/medium/high. Unset = provider default.",
    )
    timeout_seconds: float = Field(default=60.0, ge=1)


class TTSSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUSTOM_TTS_", extra="ignore")

    provider: str = Field(default="local_api", description="local_api or generic")
    url: str = Field(..., description="External TTS endpoint")
    access_token: str | None = Field(default=None, description="Bearer token for the TTS API")
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
        default="أجب بالعربية في أقل من 40 كلمة، وحاول الإجابة مباشرة عن سؤال المستخدم."
    )
    system_prompt_file: str | None = Field(
        default=None,
        description="Path to a file whose contents replace system_prompt at startup.",
    )
    explicit_eos_mode: bool = Field(default=False)
    explicit_eos_topic: str = Field(default="eval.eos")
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

    @model_validator(mode="after")
    def _load_prompt_file(self) -> "AgentSettings":
        if self.system_prompt_file:
            self.system_prompt = Path(self.system_prompt_file).read_text(encoding="utf-8")
        return self
