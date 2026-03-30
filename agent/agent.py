from __future__ import annotations

import logging
from collections.abc import AsyncIterable

from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, ModelSettings, cli, llm
from livekit.agents.llm import ChatContext
from livekit.agents.stt import SpeechData, SpeechEvent, SpeechEventType
from livekit.plugins import silero

from config import AgentSettings, LLMSettings, STTSettings, TTSSettings
from plugins.custom_llm import CustomLLMAdapter
from plugins.custom_stt import CustomSTTAdapter
from plugins.custom_tts import CustomTTSAdapter

try:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel
except ImportError:  # pragma: no cover - optional dependency surface
    MultilingualModel = None


logger = logging.getLogger("nusuk-agent")
logging.basicConfig(level=logging.INFO)

server = AgentServer()


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
    def __init__(
        self,
        *,
        agent_settings: AgentSettings,
        stt_adapter: CustomSTTAdapter,
        llm_adapter: CustomLLMAdapter,
        tts_adapter: CustomTTSAdapter,
    ) -> None:
        super().__init__(instructions=agent_settings.system_prompt)
        self.agent_settings = agent_settings
        self.stt_adapter = stt_adapter
        self.llm_adapter = llm_adapter
        self.tts_adapter = tts_adapter

    async def stt_node(
        self,
        audio: AsyncIterable[rtc.AudioFrame],
        model_settings: ModelSettings,
    ) -> AsyncIterable[SpeechEvent]:
        del model_settings
        frames = [frame async for frame in audio]
        result = await self.stt_adapter.transcribe_frames(frames)
        data = SpeechData(language=result.language, text=result.text)
        yield SpeechEvent(type=SpeechEventType.START_OF_SPEECH, request_id=result.request_id)
        yield SpeechEvent(
            type=SpeechEventType.FINAL_TRANSCRIPT,
            request_id=result.request_id,
            alternatives=[data],
        )
        yield SpeechEvent(
            type=SpeechEventType.END_OF_SPEECH,
            request_id=result.request_id,
            alternatives=[data],
        )

    async def llm_node(
        self,
        chat_ctx: ChatContext,
        tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> AsyncIterable[str]:
        del tools, model_settings
        async for chunk in self.llm_adapter.stream_chat(chat_ctx):
            yield chunk

    async def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: ModelSettings,
    ) -> AsyncIterable[rtc.AudioFrame]:
        del model_settings
        buffered_text = []
        async for chunk in text:
            buffered_text.append(chunk)
        async for frame in self.tts_adapter.synthesize("".join(buffered_text)):
            yield frame


@server.rtc_session(agent_name=AgentSettings().name)
async def entrypoint(ctx: JobContext) -> None:
    agent_settings = AgentSettings()
    stt_settings = STTSettings()
    llm_settings = LLMSettings()
    tts_settings = TTSSettings()

    await ctx.connect()

    stt_adapter = CustomSTTAdapter(stt_settings)
    llm_adapter = CustomLLMAdapter(llm_settings, agent_settings)
    tts_adapter = CustomTTSAdapter(tts_settings)

    agent = NusukAgent(
        agent_settings=agent_settings,
        stt_adapter=stt_adapter,
        llm_adapter=llm_adapter,
        tts_adapter=tts_adapter,
    )

    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        turn_detection=ctx.proc.userdata.get("turn_detection"),
        min_endpointing_delay=agent_settings.min_endpointing_delay,
        max_endpointing_delay=agent_settings.max_endpointing_delay,
    )

    try:
        await session.start(room=ctx.room, agent=agent)
        await session.say(agent_settings.greeting)
        await ctx.wait_for_shutdown()
    finally:
        await stt_adapter.aclose()
        await llm_adapter.aclose()
        await tts_adapter.aclose()


if __name__ == "__main__":
    cli.run_app(server)
