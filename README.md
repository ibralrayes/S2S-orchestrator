# S2S-orchestrator

Self-hosted realtime speech-to-speech orchestration built around LiveKit.

This repository is the control plane for a voice agent system. It does not host ASR, LLM, or TTS models itself. Instead, it provides:

- a self-hosted LiveKit server for realtime media transport
- a Python LiveKit agent worker that orchestrates STT -> LLM -> TTS
- a token server for frontend authentication
- custom HTTP adapters that call external ASR, LLM, and TTS services

The goal is to keep media/session orchestration here while leaving model serving in separate repos and on separate infrastructure.

## Architecture

```text
Frontend client
   |
   | WebRTC + JWT
   v
LiveKit Server <------------------------------+
   |                                          |
   | room/session audio                       |
   v                                          |
Agent Worker (LiveKit Agents SDK)             |
   |                                          |
   | custom HTTP adapters                     |
   +--> STT endpoint  ------------------------+
   +--> LLM endpoint  ------------------------+
   +--> TTS endpoint  ------------------------+
   |
   v
Realtime audio response back to client
```

## Scope

In scope:

- LiveKit server configuration and local stack
- agent session lifecycle and VAD-based turn handling
- outbound calls to external ASR, LLM, and TTS services
- client token generation for frontend teams

Out of scope:

- frontend application code
- model training or Triton model serving
- long-term persistence and analytics pipelines
- production-grade autoscaling or Kubernetes manifests

## Current status

This repo currently provides a working foundation rather than a finished product:

- Compose stack for `redis`, `livekit-server`, `agent`, and `token-server`
- FastAPI token service with `GET /token` and `GET /health`
- LiveKit agent skeleton with Silero VAD prewarm
- custom adapter modules for practical external STT, LLM, and TTS API shapes
- LiveKit-managed session lifecycle and conversation history

What is not finished yet:

- adapter alignment with your exact backend request/response contracts
- streaming TTS from the external service
- production observability and persistence
- integration tests against real endpoints

## Repository layout

```text
S2S-orchestrator/
├── docker-compose.yml          # Local stack: redis + livekit + agent + token-server
├── .env.example                # Environment variables for all services
├── Makefile                    # Common local commands
├── TODO.md                     # Active implementation checklist
├── livekit-server/
│   └── livekit.yaml            # LiveKit server configuration
├── agent/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── agent.py                # LiveKit agent entrypoint and session wiring
│   ├── config.py               # Pydantic settings for STT/LLM/TTS/agent config
│   └── plugins/
│       ├── custom_stt.py       # Whisper/OpenAI-style transcription adapter
│       ├── custom_llm.py       # OpenAI-compatible chat completions adapter
│       └── custom_tts.py       # Speech synthesis adapter
└── token-server/
    ├── Dockerfile
    ├── requirements.txt
    └── server.py               # JWT token service for LiveKit clients
```

## How the system works

1. A frontend requests a JWT from the token server.
2. The frontend connects to LiveKit using that token.
3. The agent worker joins the room and starts a LiveKit `AgentSession`.
4. User audio is segmented by VAD.
5. The agent sends the utterance to the external STT service.
6. The transcript and chat context are sent to the external LLM service.
7. The LLM response text is sent to the external TTS service.
8. Synthesized audio is streamed or chunked back into the LiveKit room.

## Prerequisites

- Docker and Docker Compose
- reachable external ASR, LLM, and TTS endpoints
- a `.env` file based on `.env.example`

If your external services are not on the same Docker network, use hostnames or IPs that are reachable from inside the containers.

## Quick start

### 1. Create the environment file

```bash
cp .env.example .env
```

Edit `.env` and set:

- `LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET`
- the external `CUSTOM_STT_URL`
- the external `CUSTOM_LLM_URL`
- the external `CUSTOM_TTS_URL`

### 2. Start the stack

```bash
docker compose up --build
```

Or with the Makefile:

```bash
make up
```

### 3. Request a client token

```bash
curl "http://localhost:8080/token?room=test-room&identity=test-user"
```

Example response:

```json
{
  "token": "<jwt>",
  "url": "ws://localhost:7880",
  "room": "test-room",
  "identity": "test-user"
}
```

### 4. Connect the frontend

The frontend should:

- call `GET /token`
- use the returned `url`
- connect to the returned `room` with the returned `token`

## Services

### LiveKit server

Defined in `livekit-server/livekit.yaml`.

Exposed ports:

- `7880` HTTP/WebSocket signaling
- `7881` TCP fallback for RTC
- `50000-50100/udp` WebRTC media

The local config uses:

- Redis for shared state
- `--dev` mode for easier local development
- a narrow UDP range for predictable local networking

### Agent worker

Defined in `agent/agent.py`.

Key behaviors:

- preloads Silero VAD once per worker process
- starts a LiveKit `AgentSession` per room
- overrides `stt_node`, `llm_node`, and `tts_node`
- relies on LiveKit `AgentSession` for session lifecycle and conversation history
- sends an initial Arabic greeting after session startup

Current implementation details:

- STT is turn-based, not streaming
- LLM supports streamed token output from an OpenAI-compatible SSE endpoint
- TTS currently buffers the full text response before synthesis

### Token server

Defined in `token-server/server.py`.

Endpoints:

- `GET /health`
- `GET /token?room=<room>&identity=<identity>`

If `room` or `identity` are omitted, the token server generates them automatically.

## Session lifecycle and history

LiveKit already handles the realtime session lifecycle for this app.

- `AgentSession` is the main session orchestrator for the agent pipeline.
- `RoomIO` is created automatically and manages room media wiring by default.
- the linked participant is managed by LiveKit unless you override it with room options
- conversation history is already tracked by the session and exposed through session events and `session.history`

This means we do not need a separate in-memory application session store just to mirror transcripts. Add your own persistence layer only if you want history outside the lifetime of the LiveKit session.

## Adapter contracts

These are the assumptions the current code makes about the external services.

### STT adapter

File: `agent/plugins/custom_stt.py`

Expected request:

- `POST` to `CUSTOM_STT_URL`
- `multipart/form-data`
- fields:
  - `file`: WAV audio
  - `model`
  - `language`

Expected response JSON:

```json
{
  "text": "مرحبا"
}
```

The adapter also accepts `transcript` or `transcription` as fallback response keys.

### LLM adapter

File: `agent/plugins/custom_llm.py`

Expected request:

- `POST` to `CUSTOM_LLM_URL + /chat/completions` unless the URL already ends with `/chat/completions`
- OpenAI-compatible JSON payload
- `stream=true`

Expected streaming response:

- SSE `data:` lines
- OpenAI-style delta chunks under `choices[0].delta.content`

### TTS adapter

File: `agent/plugins/custom_tts.py`

Expected request:

- `POST` to `CUSTOM_TTS_URL`
- JSON body with:
  - `model`
  - `voice`
  - `input`
  - `response_format`

Supported response formats today:

- `wav`
- raw `pcm`

The current implementation chunks returned audio into 10 ms `rtc.AudioFrame`s for playback into LiveKit.

## Configuration

### LiveKit

- `LIVEKIT_URL`
  internal URL used by containers and the agent worker
- `LIVEKIT_PUBLIC_URL`
  URL returned to frontend clients by the token server
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`

For local Docker development, the defaults are:

- internal: `ws://livekit-server:7880`
- public: `ws://localhost:7880`

### Token server

- `TOKEN_SERVER_PORT`
- `TOKEN_TTL_MINUTES`
- `TOKEN_CORS_ORIGINS`

### Agent behavior

- `AGENT_NAME`
- `AGENT_DISPLAY_NAME`
- `AGENT_IDENTITY_PREFIX`
- `AGENT_SYSTEM_PROMPT`
- `AGENT_GREETING`
- `AGENT_USE_TURN_DETECTOR`
- `AGENT_VAD_ACTIVATION_THRESHOLD`
- `AGENT_MIN_ENDPOINTING_DELAY`
- `AGENT_MAX_ENDPOINTING_DELAY`

### External services

STT:

- `CUSTOM_STT_URL`
- `CUSTOM_STT_MODEL`
- `CUSTOM_STT_LANGUAGE`
- `CUSTOM_STT_TIMEOUT_SECONDS`
- `CUSTOM_STT_TARGET_SAMPLE_RATE`

LLM:

- `CUSTOM_LLM_URL`
- `CUSTOM_LLM_MODEL`
- `CUSTOM_LLM_TEMPERATURE`
- `CUSTOM_LLM_MAX_TOKENS`
- `CUSTOM_LLM_TIMEOUT_SECONDS`

TTS:

- `CUSTOM_TTS_URL`
- `CUSTOM_TTS_MODEL`
- `CUSTOM_TTS_VOICE`
- `CUSTOM_TTS_SAMPLE_RATE`
- `CUSTOM_TTS_NUM_CHANNELS`
- `CUSTOM_TTS_AUDIO_FORMAT`
- `CUSTOM_TTS_TIMEOUT_SECONDS`

## Development workflow

Validate Python syntax:

```bash
make validate
```

Tail logs:

```bash
make logs
```

Stop the stack:

```bash
make down
```

## Known limitations

- The agent depends on the external services matching the documented adapter contracts.
- STT is currently single-turn rather than incremental streaming.
- TTS currently starts after the full LLM response is collected.
- Session history is stored in memory only and is cleared when a room ends.
- This repo has not yet been exercised end-to-end against the real ASR, LLM, and TTS services.

## Recommended next steps

1. Replace the placeholder endpoint URLs in `.env`.
2. Confirm the exact payload and response shape for ASR, LLM, and TTS.
3. Bring the stack up locally and test the token flow.
4. Tighten the adapters against the real services.
5. Add streaming TTS and deeper interruption handling once the first end-to-end call works.
