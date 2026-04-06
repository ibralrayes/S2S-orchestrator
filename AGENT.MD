# S2S-orchestrator Agent Notes

## Project goal

Build a fully self-hosted realtime speech-to-speech orchestrator around LiveKit.

This repo is the control plane. It should:

- accept realtime audio from clients through LiveKit
- run STT -> LLM -> TTS orchestration
- keep session and turn handling inside LiveKit `AgentSession`
- call external self-hosted ASR, LLM, and TTS services over HTTP
- return low-latency synthesized speech back to the user

This repo should not become a model-serving repo. Model inference belongs in separate services and repositories.

## Current architecture

- `livekit-server/`
  Local LiveKit server config for development.
- `token-server/`
  FastAPI service that issues LiveKit JWTs to frontend clients.
- `agent/`
  LiveKit agent worker and custom adapters.
- `agent/plugins/custom_stt.py`
  Turn-based STT adapter. Current assumption is Whisper/OpenAI-style transcription over multipart WAV upload.
- `agent/plugins/custom_llm.py`
  Streaming LLM adapter. Supports:
  - OpenAI-style `/chat/completions`
  - Nusuk `/chat/stream`
- `agent/plugins/custom_tts.py`
  TTS adapter. Current assumption is JSON request and WAV/PCM response.

## Important design decisions

- LiveKit handles room/session lifecycle.
- `AgentSession` is the source of truth for realtime session state and conversation history.
- We intentionally removed any custom in-memory session store.
- Room I/O options are explicit in code instead of relying on defaults.
- Stable room defaults are hard-coded in `agent/agent.py`:
  - audio input enabled
  - audio output enabled
  - text input disabled
  - text output disabled
  - input sample rate `24000`
  - mono audio
  - `50 ms` frames
- STT is currently turn-based, not incremental streaming.
- TTS currently buffers the full response before synthesis.

## Current LLM status

Nusuk chat API has been validated directly against:

- `POST /chat`
- `POST /chat/stream`

Important integration behavior:

- Nusuk is session-based, not OpenAI chat-history-based.
- In Nusuk mode, the adapter sends:
  - `query`
  - `session_id` = LiveKit room name
  - `user_id` = linked participant identity when available
  - `language`
  - `include_metadata`
  - `tool`
- Nusuk streaming returns SSE with one metadata event and then repeated `data: {"delta":"..."}` events.

## Product constraints

- Everything should remain self-hosted or locally operated.
- Avoid reinventing transport/session infrastructure when LiveKit already covers it.
- Keep adapters simple and explicit.
- Prefer practical correctness over premature abstractions.
- Only add persistence if product requirements actually need it.

## Known gaps

- Real ASR endpoint contract still needs final verification.
- Real TTS endpoint contract and streaming capability still need final verification.
- End-to-end LiveKit run with real external services is still pending.
- TTS should become streaming for better realtime UX.
- Manual event logging in `agent.py` can likely be replaced with `metrics_collected` once observability is wired properly.

## Near-term priorities

1. Confirm ASR request/response contract and audio format requirements.
2. Confirm TTS request/response contract and whether streaming audio is supported.
3. Run the full stack locally with real service credentials.
4. Replace buffered TTS with streaming TTS if the backend supports it.
5. Add structured metrics and latency reporting.

## Implementation guidance for future agents

- Do not add a new application-level session store unless there is a clear product requirement.
- Do not move model inference into this repo.
- If changing LLM integration, preserve both provider modes unless intentionally removing one:
  - `openai`
  - `nusuk`
- If changing room behavior, check current LiveKit docs before assuming defaults.
- Keep commit messages short and specific.
- When editing files, preserve any user changes you did not make.

## Good next tasks

- wire real ASR contract into `custom_stt.py`
- wire real TTS contract into `custom_tts.py`
- add a small benchmark script for LLM/TTS latency
- replace transcript/content `logger.info` hooks with `metrics_collected` if only observability is needed
