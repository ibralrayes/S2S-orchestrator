# LiveKit — SDK Patterns and Implementation Notes

## Version

`livekit-agents` Python SDK. The agent uses `AgentServer`, `AgentSession`, `Agent`, `JobContext`, `cli`, `llm`, `stt`, `tts`, `room_io`.

## How LiveKit Dispatches Jobs

1. A browser client connects to the LiveKit server with a JWT that includes a `RoomConfiguration` with an `agents` dispatch entry naming the agent.
2. The LiveKit server creates the room and dispatches a job to the registered worker pool.
3. A worker process is assigned the job and calls the `@server.rtc_session` decorated function.
4. The agent calls `ctx.connect()` to join the room as a participant.

The agent is **not** a participant kind `0` (standard) — it is kind `4` (`_AGENT_PARTICIPANT_KIND = 4`). This constant is used to filter out agent audio tracks from user audio listeners.

## `AgentServer` and `server.setup_fnc`

```python
server = AgentServer()
server.setup_fnc = prewarm   # called once per worker process before any jobs
```

`prewarm` is invoked synchronously by the SDK (not awaited), so it must be `def` not `async def`. It starts the Prometheus metrics server, initializes Langfuse (see [observability.md](observability.md)), loads Silero VAD into `proc.userdata["vad"]`, and (for the nusuk provider) pre-fetches a JWT via `asyncio.run(...)`. Everything in `proc.userdata` is shared across all sessions handled by that worker.

## `AgentSession`

The central orchestrator. Wires together STT, LLM, TTS, VAD, and turn detection into a pipeline.

```python
session = AgentSession(
    stt=streaming_stt,       # livekit.agents.stt.StreamAdapter wrapping CustomSTTAdapter
    llm=llm_provider,        # CustomLLM instance
    tts=tts_provider,        # CustomTTS instance
    vad=ctx.proc.userdata["vad"],
    turn_detection=turn_detection,   # MultilingualModel or None
    ...
)
await session.start(room=ctx.room, agent=agent, room_options=...)
```

### Sentence Buffering (LLM → TTS)

When the LLM streams tokens, `AgentSession` accumulates them into a buffer. When a sentence boundary is detected, it flushes the buffer to TTS immediately. This means TTS starts for sentence 1 while the LLM is still generating sentence 2. This is the primary LiveKit latency advantage over non-streaming PTT.

Sentence boundaries detected: `.`, `،`, `؟`, `!`, newline. **The LLM must emit proper punctuation for this to work.**

### Turn Detection

`AgentSession` integrates `MultilingualModel` for semantic end-of-turn detection. If not installed or if the language is unsupported (e.g., Arabic), it falls back to VAD-based silence detection using `min_endpointing_delay` and `max_endpointing_delay`.

VAD overhead on post-speech turns (measured): +0.5s to +3s depending on audio content and silence length.

## `stt.StreamAdapter`

Wraps a non-streaming STT (like `CustomSTTAdapter`) to present a streaming interface to `AgentSession`. VAD segments the audio stream; when VAD detects end-of-speech, it flushes the buffered frames to `_recognize_impl`.

```python
streaming_stt = stt.StreamAdapter(stt=stt_adapter, vad=ctx.proc.userdata["vad"])
```

**Important:** `_recognize_impl` must accept `conn_options` as a keyword argument (even if unused). Renaming it breaks the SDK call:
```python
# CORRECT
async def _recognize_impl(self, buffer, *, language=None, conn_options=None):
    ...
```

## `room_io.RoomOptions`

Controls how the agent's I/O is wired to the room. Key options used here:

```python
room_io.RoomOptions(
    text_input=False,                        # voice-only, no text channel
    audio_input=room_io.AudioInputOptions(
        sample_rate=16000,                   # native rate for Silero VAD + ASR; no agent-side resample
        num_channels=1,
        frame_size_ms=50,
        pre_connect_audio=True,              # buffer audio before session is fully ready
        pre_connect_audio_timeout=3.0,
    ),
    audio_output=room_io.AudioOutputOptions(...),
    text_output=room_io.TextOutputOptions(
        sync_transcription=False,            # don't gate text output on audio timing
    ),
    close_on_disconnect=True,
    delete_room_on_close=False,
)
```

## Writing a Custom LLM

Must extend `llm.LLM` and return an `llm.LLMStream` subclass from `chat()`.

The stream must implement `_run()`, which sends `llm.ChatChunk` events to `self._event_ch`:

```python
self._event_ch.send_nowait(
    llm.ChatChunk(
        id=request_id,
        delta=llm.ChoiceDelta(role="assistant", content=token_text),
    )
)
```

Unused parameters in `chat()` should be deleted:
```python
del parallel_tool_calls, tool_choice, extra_kwargs
```

## Writing a Custom STT

Must extend `stt.STT` with `streaming=False` capabilities.

```python
super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False, diarization=False))
```

Must implement `_recognize_impl(buffer, *, language=None, conn_options=None)` — note: `conn_options` must keep its original name (SDK passes it as a keyword argument).

Return a `stt.SpeechEvent` with `type=stt.SpeechEventType.FINAL_TRANSCRIPT`.

## Writing a Custom TTS

Must extend `tts.TTS` with `streaming=False`.

Return a `tts.ChunkedStream` subclass from `synthesize()`. The stream implements `_run(output_emitter: AudioEmitter)`:

```python
output_emitter.initialize(
    request_id=...,
    sample_rate=...,
    num_channels=...,
    mime_type="audio/pcm",
    frame_size_ms=20,
)
output_emitter.push(pcm_bytes)
```

On HTTP error, initialize the emitter with empty push (don't raise — the session survives).

## Participant Kind Constants

LiveKit uses integer participant kinds internally:

| Kind | Value | Meaning |
|---|---|---|
| Standard | 0 | Regular participant |
| Ingress | 1 | Ingress participant |
| Egress | 2 | Egress participant |
| SIP | 3 | SIP participant |
| Agent | 4 | Agent worker |

`_AGENT_PARTICIPANT_KIND = 4` is used in track subscription handlers to skip agent audio.

## Token Generation

Tokens are JWTs signed with `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET`. The `RoomConfiguration` embedded in the token tells LiveKit which agent to dispatch.

```python
cfg = RoomConfiguration()
dispatch = cfg.agents.add()
dispatch.agent_name = "nusuk-agent"

token = (
    AccessToken(api_key, api_secret)
    .with_identity(identity)
    .with_grants(VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True))
    .with_room_config(cfg)
    .to_jwt()
)
```

The Next.js demo token route (`/api/token`) does the same thing with the TypeScript SDK.

## Room Events Used

```python
ctx.room.on("disconnected")   # set Event to unblock entrypoint
ctx.room.on("track_subscribed")  # detect user audio track (explicit EOS mode)
ctx.room.on("data_received")     # detect __EOS__ signal (explicit EOS mode)
```

```python
session.on("user_input_transcribed")   # log final transcript
session.on("conversation_item_added")  # log assistant reply
```

## Public IP Requirement (Production)

WebRTC requires LiveKit to know its public IP. In `livekit-server/livekit.yaml`:

```yaml
rtc:
  use_external_ip: true   # auto-detect from cloud instance metadata
  # or:
  node_ip: <your-public-ip>
```

Without this, signaling works but no audio flows. See [troubleshooting.md](troubleshooting.md#livekit-public-ip).
