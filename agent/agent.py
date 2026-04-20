from __future__ import annotations

import asyncio
import logging
import os

import httpx
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli, llm, stt
from livekit.agents.voice import room_io
from livekit.plugins import silero

import metrics
from config import AgentSettings, LLMSettings, STTSettings, TTSSettings
from plugins.custom_llm import CustomLLM
from plugins.custom_stt import CustomSTTAdapter
from plugins.custom_tts import CustomTTS
from plugins.nusuk_auth import NusukTokenManager

try:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel
except ImportError:  # pragma: no cover - optional dependency
    MultilingualModel = None


logger = logging.getLogger("nusuk-agent")
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("livekit.agents").setLevel(logging.DEBUG)

# Maximum rooms per worker process. Above this fraction the worker stops accepting jobs.
_MAX_JOBS_PER_WORKER = int(os.getenv("AGENT_MAX_JOBS_PER_WORKER", "10"))

server = AgentServer()
server.load_threshold = 0.8
server.load_fnc = lambda s: min(len(s.active_jobs) / _MAX_JOBS_PER_WORKER, 1.0)

# LiveKit participant kind value for agent processes (internal SDK constant).
_AGENT_PARTICIPANT_KIND = 4


async def prewarm(proc: agents.JobProcess) -> None:
    settings = AgentSettings()
    llm_settings = LLMSettings()

    # Start Prometheus metrics HTTP server — once per worker process.
    metrics_port = int(os.getenv("AGENT_METRICS_PORT", "9090"))
    metrics.start_server(metrics_port)

    # Load Silero VAD model — shared across all sessions in this worker process.
    proc.userdata["vad"] = silero.VAD.load(
        activation_threshold=settings.vad_activation_threshold
    )

    # Pre-fetch Nusuk JWT so the first room doesn't pay an auth RTT before
    # its first LLM call. The token manager is shared across all sessions
    # handled by this worker process.
    if (
        llm_settings.provider.strip().lower() == "nusuk"
        and llm_settings.client_id
        and llm_settings.client_secret
    ):
        shared_http_client = httpx.AsyncClient(timeout=llm_settings.timeout_seconds)
        token_manager = NusukTokenManager(
            base_url=llm_settings.url,
            client_id=llm_settings.client_id,
            client_secret=llm_settings.client_secret,
            client=shared_http_client,
        )
        try:
            await token_manager.get_token()
            logger.info("prewarm nusuk_token_prefetched")
        except Exception:
            logger.warning("prewarm nusuk_token_prefetch_failed", exc_info=True)
        proc.userdata["nusuk_token_manager"] = token_manager


server.setup_fnc = prewarm


class NusukAgent(Agent):
    def __init__(self, *, agent_settings: AgentSettings) -> None:
        super().__init__(instructions=agent_settings.system_prompt)


def _build_room_options(
    agent_settings: AgentSettings, tts_settings: TTSSettings
) -> room_io.RoomOptions:
    # ── Room I/O defaults ──────────────────────────────────────────────────────
    # These are intentional fixed values; they don't need to be env-configurable.
    return room_io.RoomOptions(
        text_input=False,  # disable text input channel (voice-only)
        audio_input=room_io.AudioInputOptions(
            sample_rate=24000,        # Hz — must match VAD/STT expectations
            num_channels=1,           # mono
            frame_size_ms=50,         # audio capture granularity
            pre_connect_audio=True,   # buffer audio before session is fully ready
            pre_connect_audio_timeout=3.0,  # seconds to wait for pre-connect audio
        ),
        audio_output=room_io.AudioOutputOptions(
            sample_rate=tts_settings.sample_rate,   # match TTS output format
            num_channels=tts_settings.num_channels,
        ),
        text_output=room_io.TextOutputOptions(
            sync_transcription=False,    # don't gate text output on audio timing
            transcription_speed_factor=1.0,
        ),
        close_on_disconnect=agent_settings.close_on_disconnect,
        delete_room_on_close=agent_settings.delete_room_on_close,
        **({"participant_identity": agent_settings.participant_identity}
           if agent_settings.participant_identity else {}),
    )


def _extract_text(value: object) -> str:
    """Extract a plain string from an LLM content value (str or list of chunks)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            text = item if isinstance(item, str) else (
                item.get("text") if isinstance(item, dict) else None
            )
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
        return " ".join(parts)
    return ""


def _resolve_user_identity(ctx: JobContext, agent_settings: AgentSettings) -> str | None:
    if agent_settings.participant_identity:
        return agent_settings.participant_identity
    remote_participants = list(ctx.room.remote_participants.values())
    if remote_participants:
        return remote_participants[0].identity
    return None


async def _aclose_providers(
    stt_adapter: CustomSTTAdapter,
    llm_provider: CustomLLM,
    tts_provider: CustomTTS,
) -> None:
    """Close all three HTTP clients in order."""
    await stt_adapter.aclose()
    await llm_provider.aclose()
    await tts_provider.aclose()


async def _collect_llm_reply(llm_provider: CustomLLM, user_text: str) -> str:
    chat_ctx = llm.ChatContext()
    chat_ctx.add_message(role="user", content=user_text)
    stream = llm_provider.chat(chat_ctx=chat_ctx, tools=[])
    parts: list[str] = []
    async for chunk in stream:
        delta = getattr(chunk, "delta", None)
        if delta and getattr(delta, "content", None):
            parts.append(delta.content)
    return "".join(parts).strip()


async def _publish_tts_reply(
    audio_source: rtc.AudioSource,
    tts_provider: CustomTTS,
    reply_text: str,
) -> None:
    stream = tts_provider.synthesize(reply_text)
    async for ev in stream:
        await audio_source.capture_frame(ev.frame)


async def _run_explicit_eos_mode(
    ctx: JobContext,
    *,
    agent_settings: AgentSettings,
    stt_adapter: CustomSTTAdapter,
    llm_provider: CustomLLM,
    tts_provider: CustomTTS,
    tts_settings: TTSSettings,
) -> None:
    logger.info("room=%s explicit_eos_mode=enabled", ctx.room.name)
    audio_source = rtc.AudioSource(
        sample_rate=tts_settings.sample_rate,
        num_channels=tts_settings.num_channels,
    )
    local_track = rtc.LocalAudioTrack.create_audio_track("assistant", audio_source)
    await ctx.room.local_participant.publish_track(
        local_track,
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )

    buffered_frames: list[rtc.AudioFrame] = []
    frames_lock = asyncio.Lock()
    reply_lock = asyncio.Lock()
    drain_tasks: list[asyncio.Task[None]] = []
    disconnected = asyncio.Event()
    ctx.room.on("disconnected")(lambda *_: disconnected.set())

    async def _drain_user_audio(track: rtc.RemoteAudioTrack) -> None:
        stream = rtc.AudioStream(track)
        async for frame_event in stream:
            frame = frame_event.frame
            copied = rtc.AudioFrame(
                data=bytearray(bytes(frame.data)),
                sample_rate=frame.sample_rate,
                num_channels=frame.num_channels,
                samples_per_channel=frame.samples_per_channel,
            )
            async with frames_lock:
                buffered_frames.append(copied)

    async def _handle_explicit_eos() -> None:
        async with reply_lock:
            async with frames_lock:
                frames = list(buffered_frames)
                buffered_frames.clear()
            if not frames:
                logger.warning("room=%s explicit_eos_no_frames", ctx.room.name)
                return

            stt_result = await stt_adapter.transcribe_frames(frames)
            transcript = stt_result.text.strip()
            if not transcript:
                logger.warning("room=%s explicit_eos_empty_transcript", ctx.room.name)
                return

            logger.info("room=%s event=user_input_transcribed transcript=%s", ctx.room.name, transcript)
            reply_text = await _collect_llm_reply(llm_provider, transcript)
            if not reply_text:
                logger.warning("room=%s explicit_eos_empty_reply", ctx.room.name)
                return

            logger.info("room=%s event=conversation_item_added role=assistant content=%s", ctx.room.name, reply_text)
            await _publish_tts_reply(audio_source, tts_provider, reply_text)

    @ctx.room.on("track_subscribed")
    def _on_track_subscribed(track, _publication, participant) -> None:
        if participant.kind == _AGENT_PARTICIPANT_KIND:
            return
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return
        drain_tasks.append(asyncio.create_task(_drain_user_audio(track)))

    @ctx.room.on("data_received")
    def _on_data_received(packet) -> None:
        if getattr(packet, "topic", "") != agent_settings.explicit_eos_topic:
            return
        if getattr(packet, "participant", None) is None:
            return
        if packet.participant.kind == _AGENT_PARTICIPANT_KIND:
            return
        payload = packet.data.decode("utf-8", errors="ignore").strip()
        if payload != "__EOS__":
            return
        drain_tasks.append(asyncio.create_task(_handle_explicit_eos()))

    try:
        await disconnected.wait()
    finally:
        for task in drain_tasks:
            task.cancel()
        await audio_source.wait_for_playout()


@server.rtc_session(agent_name=AgentSettings().name)
async def entrypoint(ctx: JobContext) -> None:
    agent_settings = AgentSettings()
    stt_settings = STTSettings()
    llm_settings = LLMSettings()
    tts_settings = TTSSettings()

    await ctx.connect()

    stt_adapter = CustomSTTAdapter(stt_settings)
    llm_provider = CustomLLM(
        llm_settings,
        agent_settings,
        session_id=ctx.room.name,
        user_id=_resolve_user_identity(ctx, agent_settings),
    )
    tts_provider = CustomTTS(tts_settings)

    if agent_settings.explicit_eos_mode:
        try:
            await _run_explicit_eos_mode(
                ctx,
                agent_settings=agent_settings,
                stt_adapter=stt_adapter,
                llm_provider=llm_provider,
                tts_provider=tts_provider,
                tts_settings=tts_settings,
            )
        finally:
            await _aclose_providers(stt_adapter, llm_provider, tts_provider)
        return

    streaming_stt = stt.StreamAdapter(stt=stt_adapter, vad=ctx.proc.userdata["vad"])
    agent = NusukAgent(agent_settings=agent_settings)

    # Turn detection always on; MultilingualModel is an optional install.
    turn_detection = MultilingualModel() if MultilingualModel is not None else None

    session = AgentSession(
        stt=streaming_stt,           # speech-to-text pipeline
        llm=llm_provider,            # language model
        tts=tts_provider,            # text-to-speech pipeline
        vad=ctx.proc.userdata["vad"],  # voice activity detector (preloaded in prewarm)
        turn_detection=turn_detection,  # semantic end-of-turn model; None = VAD-only
        # ── Interruption handling ──────────────────────────────────────────────
        allow_interruptions=agent_settings.allow_interruptions,
        # whether to discard buffered TTS audio when the user interrupts
        discard_audio_if_uninterruptible=agent_settings.discard_audio_if_uninterruptible,
        # minimum seconds of user speech before treating it as an interruption
        min_interruption_duration=agent_settings.min_interruption_duration,
        # minimum word count before treating user speech as an interruption
        min_interruption_words=agent_settings.min_interruption_words,
        # ── Endpointing (when to consider a turn finished) ─────────────────────
        # The shortest silence the system waits before deciding the user has finished speaking (seconds)
        min_endpointing_delay=agent_settings.min_endpointing_delay,
        # longest silence before forcing an end-of-turn even without model signal (seconds)
        max_endpointing_delay=agent_settings.max_endpointing_delay,
        # ── False-interruption recovery ────────────────────────────────────────
        # seconds to wait before deciding a brief interruption was a false positive
        false_interruption_timeout=agent_settings.false_interruption_timeout,
        # resume speaking after a false interruption is detected
        resume_false_interruption=agent_settings.resume_false_interruption,
        # minimum silence gap before treating consecutive speech segments as one turn
        min_consecutive_speech_delay=agent_settings.min_consecutive_speech_delay,
        # use TTS word-timing to align the transcript instead of real-time STT output
        use_tts_aligned_transcript=agent_settings.use_tts_aligned_transcript,
    )

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(event) -> None:
        transcript = getattr(event, "transcript", "")
        if transcript and getattr(event, "is_final", False):
            logger.info("room=%s event=user_input_transcribed transcript=%s", ctx.room.name, transcript)

    @session.on("conversation_item_added")
    def _on_conversation_item_added(event) -> None:
        item = getattr(event, "item", None)
        if item is None:
            return
        role = getattr(item, "role", None)
        if role != "assistant":
            return
        content = _extract_text(getattr(item, "content", ""))
        if content:
            logger.info("room=%s event=conversation_item_added role=%s content=%s", ctx.room.name, role, content)

    disconnected = asyncio.Event()
    ctx.room.on("disconnected")(lambda *_: disconnected.set())

    try:
        logger.info(
            "room=%s stage=session_start stt_url=%s llm_provider=%s tts_url=%s",
            ctx.room.name, stt_settings.url, llm_settings.provider, tts_settings.url,
        )
        await session.start(
            room=ctx.room,
            agent=agent,
            room_options=_build_room_options(agent_settings, tts_settings),
        )
        logger.info("room=%s stage=session_ready", ctx.room.name)
        await disconnected.wait()
    finally:
        await streaming_stt.aclose()
        await _aclose_providers(stt_adapter, llm_provider, tts_provider)


if __name__ == "__main__":
    cli.run_app(server)
