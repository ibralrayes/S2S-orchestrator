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
│       ├── custom_llm.py       # OpenAI-style and Nusuk streaming chat adapter
│       └── custom_tts.py       # Speech synthesis adapter
├── token-server/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── server.py               # JWT token service for LiveKit clients
└── demo/                       # Optional demo frontend (docker compose --profile demo)
    ├── Dockerfile
    ├── app-config.ts           # Branding and feature toggles
    └── app/api/token/route.ts  # Next.js token endpoint for the demo UI
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

The current defaults assume Nusuk for STT and Groq for the LLM. Override them if you want another provider.

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

### 5. Run the demo frontend (optional)

A pre-configured voice agent demo UI is included in `demo/`. Start it alongside the stack:

```bash
docker compose --profile demo up --build
```

Two demos share the same frontend:

#### Realtime LiveKit demo (`http://localhost:3000`)

- Full realtime pipeline through LiveKit: STT -> Nusuk chat -> TTS.
- Uses the agent's current default turn handling: Silero VAD plus the optional LiveKit `MultilingualModel` turn detector when that dependency is installed in the worker image.

#### Push-to-talk demo (`http://localhost:3000/ptt`)

- Modular ASR -> Nusuk chat -> TTS. The browser records with `MediaRecorder`, the Next.js API routes under `/api/ptt/*` proxy to the services, and Nusuk auth is handled server-side with an auto-refreshing JWT (see `demo/lib/nusukAuth.ts`).
- Useful as a debugging fallback when LiveKit media is misbehaving: every stage (record / transcribe / think / speak) surfaces its own status chip.

Required env vars for the frontend container (populated by `docker-compose.yml` from `.env`): `ASR_URL`, `ASR_TOKEN`, `TTS_URL`, `NUSUK_URL`, `NUSUK_CLIENT_ID`, `NUSUK_CLIENT_SECRET`.

The demo uses LiveKit's open-source [Agent Starter React](https://github.com/livekit-examples/agent-starter-react) template with the Agents UI component library.

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
- starts the session with explicit `RoomOptions` instead of relying on implicit defaults
- overrides `stt_node`, `llm_node`, and `tts_node`
- relies on LiveKit `AgentSession` for session lifecycle and conversation history
- sends an initial Arabic greeting after session startup

Current implementation details:

- STT is turn-based, not streaming
- LLM supports streamed token output from either an OpenAI-compatible SSE endpoint or Nusuk `/chat/stream`
- TTS currently buffers the full text response before synthesis
- interruption and room I/O behavior are now surfaced through environment config
- stable room I/O defaults are hard-coded to keep `.env` smaller: text/audio input enabled, text/audio output enabled, 24 kHz mono input, 50 ms frames, pre-connect audio enabled, synced text output

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

Supported provider modes:

- `CUSTOM_STT_PROVIDER=openai`
- `POST` to `CUSTOM_STT_URL`
- `multipart/form-data`
- fields:
  - `file`: WAV audio
  - `model`
  - `language`

- `CUSTOM_STT_PROVIDER=nusuk`
- `POST` to `CUSTOM_STT_URL + /transcribe` unless the URL already ends with `/transcribe`
- `Authorization: Bearer <token>`
- `multipart/form-data`
- fields:
  - `file`: WAV audio

Expected response JSON:

```json
{
  "text": "مرحبا"
}
```

The adapter also accepts Nusuk-style `transcription_text`, plus `transcript` or `transcription` as fallback response keys.

### LLM adapter

File: `agent/plugins/custom_llm.py`

Supported provider modes:

- `CUSTOM_LLM_PROVIDER=openai`
- `POST` to `CUSTOM_LLM_URL + /chat/completions` unless the URL already ends with `/chat/completions`
- OpenAI-compatible JSON payload
- `stream=true`

- `CUSTOM_LLM_PROVIDER=nusuk`
- `POST` to `CUSTOM_LLM_URL + /chat/stream` unless the URL already ends with `/chat` or `/chat/stream`
- JSON body with `query`, `session_id`, `language`, `include_metadata`, and `tool`
- `user_id` is sent when a linked LiveKit participant identity is available
- `session_id` is mapped from the LiveKit room name

Expected streaming response:

- SSE `data:` lines
- OpenAI-style delta chunks under `choices[0].delta.content`
- or Nusuk delta chunks under `delta`

### TTS adapter

File: `agent/plugins/custom_tts.py`

Supported provider modes today:

- `CUSTOM_TTS_PROVIDER=local_api`
- `POST` to `CUSTOM_TTS_URL + /api/synthesize` unless the URL already ends with `/api/synthesize`
- optional `Authorization: Bearer <token>`
- JSON body with:
  - `text`
  - `output_format`
  - `sample_rate`

- `CUSTOM_TTS_PROVIDER=generic`
- `POST` to `CUSTOM_TTS_URL`
- optional `Authorization: Bearer <token>`
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
- `AGENT_SYSTEM_PROMPT`
- `AGENT_GREETING`
- `AGENT_VAD_ACTIVATION_THRESHOLD`
- `AGENT_ALLOW_INTERRUPTIONS`
- `AGENT_DISCARD_AUDIO_IF_UNINTERRUPTIBLE`
- `AGENT_MIN_INTERRUPTION_DURATION`
- `AGENT_MIN_INTERRUPTION_WORDS`
- `AGENT_MIN_ENDPOINTING_DELAY`
- `AGENT_MAX_ENDPOINTING_DELAY`
- `AGENT_FALSE_INTERRUPTION_TIMEOUT`
- `AGENT_RESUME_FALSE_INTERRUPTION`
- `AGENT_MIN_CONSECUTIVE_SPEECH_DELAY`
- `AGENT_USE_TTS_ALIGNED_TRANSCRIPT`
- `AGENT_PARTICIPANT_IDENTITY`
- `AGENT_CLOSE_ON_DISCONNECT`
- `AGENT_DELETE_ROOM_ON_CLOSE`

The following room I/O defaults are hard-coded in `agent/agent.py` instead of exposed as env vars:

- text input enabled
- audio input enabled
- audio output enabled
- text output enabled
- audio input sample rate `24000`
- audio input channels `1`
- audio input frame size `50 ms`
- pre-connect audio enabled with `3.0 s` timeout
- synced text transcription enabled with speed factor `1.0`

### External services

STT:

- `CUSTOM_STT_URL`
- `CUSTOM_STT_PROVIDER`
- `CUSTOM_STT_MODEL`
- `CUSTOM_STT_ACCESS_TOKEN`
- `CUSTOM_STT_LANGUAGE`
- `CUSTOM_STT_TIMEOUT_SECONDS`
- `CUSTOM_STT_TARGET_SAMPLE_RATE`

`CUSTOM_STT_MODEL` is currently unused when `CUSTOM_STT_PROVIDER=nusuk`.

LLM:

- `CUSTOM_LLM_URL`
- `CUSTOM_LLM_PROVIDER`
- `CUSTOM_LLM_MODEL`
- `CUSTOM_LLM_ACCESS_TOKEN`
- `CUSTOM_LLM_LANGUAGE`
- `CUSTOM_LLM_INCLUDE_METADATA`
- `CUSTOM_LLM_TOOL`
- `CUSTOM_LLM_TEMPERATURE`
- `CUSTOM_LLM_MAX_TOKENS`
- `CUSTOM_LLM_TIMEOUT_SECONDS`

`CUSTOM_LLM_MODEL` is currently unused when `CUSTOM_LLM_PROVIDER=nusuk`.

TTS:

- `CUSTOM_TTS_URL`
- `CUSTOM_TTS_PROVIDER`
- `CUSTOM_TTS_ACCESS_TOKEN`
- `CUSTOM_TTS_MODEL`
- `CUSTOM_TTS_VOICE`
- `CUSTOM_TTS_SAMPLE_RATE`
- `CUSTOM_TTS_NUM_CHANNELS`
- `CUSTOM_TTS_AUDIO_FORMAT`
- `CUSTOM_TTS_TIMEOUT_SECONDS`

## Production deployment

### Recommended machine split

Run the orchestration layer on a CPU machine and all inference on a separate GPU machine:

```text
CPU machine                     GPU machine
───────────────────────         ──────────────────────
LiveKit server                  ASR   (port 8102)
Agent workers                   TTS   (port 8000)
Token server                    LLM   (if self-hosted)
```

No code changes are needed — the agent already makes pure HTTP calls to those services. Update `.env` to point at the GPU machine:

```env
CUSTOM_STT_URL=http://<gpu-machine-ip>:8102
CUSTOM_TTS_URL=http://<gpu-machine-ip>:8000
```

Remove the `host.docker.internal` values from `docker-compose.yml` and drop the `extra_hosts` entries from the agent and frontend services.

Keep both machines in the **same VPC or datacenter**. The agent calls ASR and TTS once per utterance; cross-region latency adds 20–80 ms per hop, which compounds noticeably in a voice pipeline.

### LiveKit public IP — required for WebRTC audio

This is the most common production failure. Without it, the WebSocket signaling works (the client connects) but no audio ever flows.

WebRTC requires LiveKit to advertise the public IP of the host to browsers so they can send UDP media packets. If the server is behind NAT (it usually is on any cloud provider), you must tell LiveKit its external address.

In `livekit-server/livekit.yaml`:

```yaml
rtc:
  use_external_ip: true   # auto-detect from cloud instance metadata (EC2, GCP, etc.)
```

Or hardcode it:

```yaml
rtc:
  node_ip: <your-public-ip>
```

Also open inbound UDP `50000–50100` in your firewall or security group. These are the WebRTC media ports.

### VAD: CPU only, no GPU needed

Silero VAD (preloaded in the agent worker's `prewarm` step) is a small ~2 MB model designed for real-time CPU inference. It processes each 50 ms audio frame in roughly 1–2 ms on a modern CPU core. There is no benefit to running it on GPU, and doing so would add a network hop on every audio frame.

Keep VAD and the agent workers on the CPU machine.

### Scaling to many concurrent users

Each LiveKit room dispatches one agent worker process. At 100 concurrent sessions:

| Layer | Approach |
|---|---|
| More agent workers | Add `deploy: replicas: N` to the `agent` service in `docker-compose.yml`; workers self-register and jobs are distributed automatically |
| ASR throughput | Add replicas of the ASR service on the GPU machine behind a load balancer |
| TTS throughput | Same — TTS (especially F5-TTS) is the slowest piece in the pipeline and benefits most from horizontal scaling |
| LiveKit server | A single instance handles hundreds of rooms comfortably; escalate to LiveKit Cloud or a distributed cluster only if you exceed that |

The agent process itself is I/O-bound (HTTP calls to ASR/LLM/TTS), so it scales horizontally with low overhead per added replica.

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
- The Nusuk chat API has been validated directly, but the full LiveKit path still needs an end-to-end run.

## Recommended next steps

1. Replace the placeholder endpoint URLs in `.env`.
2. Confirm the exact payload and response shape for ASR, LLM, and TTS.
3. Bring the stack up locally and test the token flow.
4. Tighten the adapters against the real services.
5. Add streaming TTS and deeper interruption handling once the first end-to-end call works.
