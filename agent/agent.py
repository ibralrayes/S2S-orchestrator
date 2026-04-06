from __future__ import annotations

import asyncio
import logging

from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli, stt
from livekit.agents.voice import room_io
from livekit.plugins import silero

from config import AgentSettings, LLMSettings, STTSettings, TTSSettings
from plugins.custom_llm import CustomLLM
from plugins.custom_stt import CustomSTTAdapter
from plugins.custom_tts import CustomTTS

try:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel
except ImportError:  # pragma: no cover - optional dependency surface
    MultilingualModel = None


logger = logging.getLogger("nusuk-agent")
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("livekit.agents").setLevel(logging.DEBUG)

server = AgentServer()

ROOM_TEXT_INPUT_ENABLED = False
ROOM_AUDIO_INPUT_ENABLED = True
ROOM_AUDIO_OUTPUT_ENABLED = True
ROOM_TEXT_OUTPUT_ENABLED = True
ROOM_INPUT_SAMPLE_RATE = 24000
ROOM_INPUT_NUM_CHANNELS = 1
ROOM_INPUT_FRAME_SIZE_MS = 50
ROOM_PRE_CONNECT_AUDIO = True
ROOM_PRE_CONNECT_AUDIO_TIMEOUT = 3.0
ROOM_SYNC_TRANSCRIPTION = False
ROOM_TRANSCRIPTION_SPEED_FACTOR = 1.0


def prewarm(proc: agents.JobProcess) -> None:
    settings = AgentSettings()
    proc.userdata["vad"] = silero.VAD.load(
        activation_threshold=settings.vad_activation_threshold
    )
    if settings.use_turn_detector and MultilingualModel is not None:
        proc.userdata["turn_detection"] = MultilingualModel()
    else:
        proc.userdata["turn_detection"] = None


server.setup_fnc = prewarm


class NusukAgent(Agent):
    def __init__(self, *, agent_settings: AgentSettings) -> None:
        super().__init__(instructions=agent_settings.system_prompt)


def _build_room_options(
    agent_settings: AgentSettings, tts_settings: TTSSettings
) -> room_io.RoomOptions:
    room_options_kwargs = {
        "text_input": room_io.TextInputOptions() if ROOM_TEXT_INPUT_ENABLED else False,
        "audio_input": (
            room_io.AudioInputOptions(
                sample_rate=ROOM_INPUT_SAMPLE_RATE,
                num_channels=ROOM_INPUT_NUM_CHANNELS,
                frame_size_ms=ROOM_INPUT_FRAME_SIZE_MS,
                pre_connect_audio=ROOM_PRE_CONNECT_AUDIO,
                pre_connect_audio_timeout=ROOM_PRE_CONNECT_AUDIO_TIMEOUT,
            )
            if ROOM_AUDIO_INPUT_ENABLED
            else False
        ),
        "audio_output": (
            room_io.AudioOutputOptions(
                sample_rate=tts_settings.sample_rate,
                num_channels=tts_settings.num_channels,
            )
            if ROOM_AUDIO_OUTPUT_ENABLED
            else False
        ),
        "text_output": (
            room_io.TextOutputOptions(
                sync_transcription=ROOM_SYNC_TRANSCRIPTION,
                transcription_speed_factor=ROOM_TRANSCRIPTION_SPEED_FACTOR,
            )
            if ROOM_TEXT_OUTPUT_ENABLED
            else False
        ),
        "close_on_disconnect": agent_settings.close_on_disconnect,
        "delete_room_on_close": agent_settings.delete_room_on_close,
    }
    if agent_settings.participant_identity:
        room_options_kwargs["participant_identity"] = agent_settings.participant_identity
    return room_io.RoomOptions(**room_options_kwargs)


def _extract_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return " ".join(part.strip() for part in parts if part.strip())
    return ""


def _resolve_user_identity(ctx: JobContext, agent_settings: AgentSettings) -> str | None:
    if agent_settings.participant_identity:
        return agent_settings.participant_identity

    remote_participants = list(ctx.room.remote_participants.values())
    if remote_participants:
        return remote_participants[0].identity

    return None


@server.rtc_session(agent_name=AgentSettings().name)
async def entrypoint(ctx: JobContext) -> None:
    agent_settings = AgentSettings()
    stt_settings = STTSettings()
    llm_settings = LLMSettings()
    tts_settings = TTSSettings()

    await ctx.connect()

    stt_adapter = CustomSTTAdapter(stt_settings)
    streaming_stt = stt.StreamAdapter(stt=stt_adapter, vad=ctx.proc.userdata["vad"])
    llm_provider = CustomLLM(
        llm_settings,
        agent_settings,
        session_id=ctx.room.name,
        user_id=_resolve_user_identity(ctx, agent_settings),
    )
    tts_provider = CustomTTS(tts_settings)

    agent = NusukAgent(agent_settings=agent_settings)

    session = AgentSession(
        stt=streaming_stt,
        llm=llm_provider,
        tts=tts_provider,
        vad=ctx.proc.userdata["vad"],
        turn_detection=ctx.proc.userdata.get("turn_detection"),
        allow_interruptions=agent_settings.allow_interruptions,
        discard_audio_if_uninterruptible=agent_settings.discard_audio_if_uninterruptible,
        min_interruption_duration=agent_settings.min_interruption_duration,
        min_interruption_words=agent_settings.min_interruption_words,
        min_endpointing_delay=agent_settings.min_endpointing_delay,
        max_endpointing_delay=agent_settings.max_endpointing_delay,
        false_interruption_timeout=agent_settings.false_interruption_timeout,
        resume_false_interruption=agent_settings.resume_false_interruption,
        min_consecutive_speech_delay=agent_settings.min_consecutive_speech_delay,
        use_tts_aligned_transcript=agent_settings.use_tts_aligned_transcript,
    )

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(event) -> None:
        transcript = getattr(event, "transcript", "")
        if transcript and getattr(event, "is_final", False):
            logger.info(
                "room=%s event=user_input_transcribed transcript=%s",
                ctx.room.name,
                transcript,
            )

    @session.on("conversation_item_added")
    def _on_conversation_item_added(event) -> None:
        item = getattr(event, "item", None)
        if item is None:
            return
        role = getattr(item, "role", None)
        if role != "assistant":
            return
        content = _extract_text(getattr(item, "content", ""))
        if role and content:
            logger.info(
                "room=%s event=conversation_item_added role=%s content=%s",
                ctx.room.name,
                role,
                content,
            )

    disconnected = asyncio.Event()
    ctx.room.on("disconnected")(lambda *_: disconnected.set())

    try:
        await session.start(
            room=ctx.room,
            agent=agent,
            room_options=_build_room_options(agent_settings, tts_settings),
        )
        await disconnected.wait()
    finally:
        await streaming_stt.aclose()
        await stt_adapter.aclose()
        await llm_provider.aclose()
        await tts_provider.aclose()


if __name__ == "__main__":
    cli.run_app(server)
