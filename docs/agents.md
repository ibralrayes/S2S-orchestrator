# Agent — Behavior and Responsibilities

## Entry Point

`agent/agent.py` — registered with LiveKit via `@server.rtc_session(agent_name=...)`.

Each incoming room job spawns a new Python worker process (forked from the pre-warmed pool). One worker = one room = one `AgentSession`.

## Startup Sequence

```
1. prewarm()     — async, called once per worker process at startup
                   a. starts Prometheus metrics HTTP server (AGENT_METRICS_PORT, default 9090)
                   b. loads Silero VAD model into proc.userdata["vad"]
                   c. if provider=nusuk + client_id+secret set:
                        fetches Nusuk JWT into proc.userdata["nusuk_token_manager"]
                        (shared token manager — all sessions on this worker reuse it)

2. entrypoint()  — called per room job
   a. ctx.connect()                 — join the LiveKit room
   b. ACTIVE_SESSIONS.inc()         — Prometheus gauge
   c. Build adapters                — STTAdapter, CustomLLM (with pre-warmed token_manager), CustomTTS
   d. Build AgentSession            — wires STT/LLM/TTS/VAD/turn detection
   e. session.start()               — attaches to room, begins listening
   f. Agent sends greeting via TTS
   g. await disconnected.wait()     — hold until user leaves
   h. finally: ACTIVE_SESSIONS.dec() + aclose all adapters
```

## Two Operating Modes

### Normal Mode (default)

Full `AgentSession` pipeline. VAD segments audio, STT transcribes, LLM generates, TTS synthesizes. Turn detection is always on.

### Explicit EOS Mode (`AGENT_EXPLICIT_EOS_MODE=true`)

Used for eval/testing. The agent does **not** use VAD. Instead it waits for a `__EOS__` data message on the `AGENT_EXPLICIT_EOS_TOPIC` channel, then processes all buffered audio at once. Used by `eval/compare.py --livekit-turn-mode explicit_eos`.

## Turn Detection

Always enabled. `MultilingualModel` (from `livekit.plugins.turn_detector`) is imported optionally:
- If installed: semantic turn detection — model predicts utterance completion
- If not installed: VAD-only fallback (silence timer)

```python
turn_detection = MultilingualModel() if MultilingualModel is not None else None
```

Note: `MultilingualModel` does not support Arabic (`ar`). The agent logs a warning but continues with VAD fallback for that language.

## AgentSession Parameters

All session parameters come from `AgentSettings` (env prefix `AGENT_`):

| Parameter | Env var | Default | Effect |
|---|---|---|---|
| `allow_interruptions` | `AGENT_ALLOW_INTERRUPTIONS` | `true` | User can interrupt agent speech |
| `discard_audio_if_uninterruptible` | `AGENT_DISCARD_AUDIO_IF_UNINTERRUPTIBLE` | `true` | Drop buffered TTS on interrupt |
| `min_interruption_duration` | `AGENT_MIN_INTERRUPTION_DURATION` | `0.5s` | Min speech to count as interrupt |
| `min_interruption_words` | `AGENT_MIN_INTERRUPTION_WORDS` | `0` | Min words to count as interrupt |
| `min_endpointing_delay` | `AGENT_MIN_ENDPOINTING_DELAY` | `0.5s` | Min silence before committing turn |
| `max_endpointing_delay` | `AGENT_MAX_ENDPOINTING_DELAY` | `5.0s` | Max silence before forcing end |
| `false_interruption_timeout` | `AGENT_FALSE_INTERRUPTION_TIMEOUT` | `2.0s` | Wait before deciding interruption was false |
| `resume_false_interruption` | `AGENT_RESUME_FALSE_INTERRUPTION` | `true` | Resume speech after false positive |
| `min_consecutive_speech_delay` | `AGENT_MIN_CONSECUTIVE_SPEECH_DELAY` | `0.0s` | Gap before merging consecutive turns |
| `use_tts_aligned_transcript` | `AGENT_USE_TTS_ALIGNED_TRANSCRIPT` | `false` | Use TTS timing to align transcript |

## Room I/O (Hard-Coded Defaults)

These values are intentionally fixed in `agent.py` and not env-configurable:

```python
audio_input:  sample_rate=24000, num_channels=1, frame_size_ms=50
              pre_connect_audio=True, pre_connect_audio_timeout=3.0s
audio_output: sample_rate=tts_settings.sample_rate, num_channels=tts_settings.num_channels
text_output:  sync_transcription=False, transcription_speed_factor=1.0
text_input:   disabled (voice-only)
```

## LLM Pipeline Detail

The agent uses `CustomLLMStream` which streams tokens from Nusuk SSE. The `AgentSession` performs **sentence buffering**: as tokens arrive, it accumulates them until a sentence boundary (`.`, `،`, `؟`, `\n`) is detected, then fires TTS on that sentence while the LLM continues streaming. This means TTS for the first sentence starts long before the full response is ready.

This only works correctly if the LLM uses proper punctuation. See `CUSTOM_LLM_QUERY_PREFIX` below.

## Nusuk-Specific Behavior

Nusuk does not accept a `system_prompt` field. To influence response style:

- `CUSTOM_LLM_QUERY_PREFIX` — prepended to every user query before sending to Nusuk
- Current value: bilingual instruction to use short sentences with proper punctuation and no markdown
- If unset: Nusuk uses its own internal Knowledge tool prompt (may return 100–200 word markdown responses)

## Markdown Stripping

`custom_tts.py` calls `_strip_markdown()` before every synthesis call:
- Removes `**bold**` / `*italic*`
- Removes `> blockquotes`
- Removes `[4]` citation markers
- Collapses `\n\n` paragraph breaks into spaces

This prevents the TTS from literally speaking `"asterisk asterisk bold asterisk asterisk"`.

## Session Events Logged

```
room=X stage=session_start stt_url=... llm_provider=... tts_url=...
room=X stage=session_ready
room=X event=user_input_transcribed transcript=...
room=X event=conversation_item_added role=assistant content=...
room=X explicit_eos_mode=enabled            (if in EOS mode)
room=X explicit_eos_empty_transcript        (warning: STT returned nothing)
room=X explicit_eos_empty_reply             (warning: LLM returned nothing)
```

## Cleanup

`_aclose_providers(stt_adapter, llm_provider, tts_provider)` is called in a `finally` block in both operating modes. It closes the three underlying `httpx.AsyncClient` instances in order. This runs even if the session errors out.

## VAD Placement

Silero VAD is preloaded in `prewarm()` into `proc.userdata["vad"]` — once per worker process, not per session. At 100 concurrent sessions, all sessions in the same worker process share one preloaded VAD instance. VAD runs on CPU; no GPU needed.

## Worker Load Control

`AgentServer` is configured with `load_threshold=0.8` and a custom `load_fnc`:

```python
server.load_fnc = lambda s: min(len(s.active_jobs) / _MAX_JOBS_PER_WORKER, 1.0)
```

`_MAX_JOBS_PER_WORKER` defaults to 10, overridable via `AGENT_MAX_JOBS_PER_WORKER`. Once a worker's load reaches 0.8 (8 active rooms by default), LiveKit stops dispatching new jobs to it and routes them to other workers or queues them.

## Prometheus Metrics

`agent/metrics.py` exposes the following metrics at `http://<agent>:$AGENT_METRICS_PORT/metrics` (default 9090):

| Metric | Type | Description |
|---|---|---|
| `agent_active_sessions_total` | Gauge | Currently active sessions |
| `agent_stt_duration_seconds` | Histogram | STT HTTP wall time |
| `agent_stt_errors_total` | Counter | STT failures |
| `agent_llm_ttft_seconds` | Histogram | LLM time-to-first-token |
| `agent_llm_duration_seconds` | Histogram | LLM total stream duration |
| `agent_llm_errors_total` | Counter | LLM failures (labelled by provider) |
| `agent_tts_duration_seconds` | Histogram | TTS synthesis wall time |
| `agent_tts_errors_total` | Counter | TTS failures |

The metrics server starts in `prewarm()` — one per worker process, with a silent fallback if the port is already bound by another worker in the same container. For multi-process accuracy in production, configure `PROMETHEUS_MULTIPROC_DIR` and use `MultiProcessCollector`.
