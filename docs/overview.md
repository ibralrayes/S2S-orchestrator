# S2S-Orchestrator — System Overview

## What This Is

A self-hosted, real-time **speech-to-speech orchestration control plane** built on LiveKit. It connects a voice user (browser) to three backend AI services — ASR, LLM, and TTS — through a managed session layer.

This repository owns **orchestration only**. It does not run any AI models. All model inference lives in separate services and is called over HTTP.

## What It Is Not

- Not a model server (no Triton, no GPU workloads here)
- Not a frontend application (a demo frontend is included but is optional)
- Not a session persistence layer (conversation history lives in the LiveKit session in memory only)

## Two User-Facing Demos

### 1. LiveKit Realtime Demo (`/`)

Full duplex voice conversation. The browser sends microphone audio over WebRTC; the agent processes it through STT → LLM → TTS and plays back synthesized speech in real time. VAD and semantic turn detection control when the pipeline triggers.

### 2. Push-to-Talk Demo (`/ptt`)

Modular pipeline driven by a hold-to-talk button. The browser records audio, then sequentially calls ASR → Nusuk chat → TTS via Next.js API proxy routes. No WebRTC. Useful as a debugging fallback and for latency comparison.

## Core Services

| Service | Role | Default Port |
|---|---|---|
| `livekit-server` | WebRTC media relay (SFU) | 7880 (WS), 7881 (TCP), 50000–50100 (UDP), 6789 (Prometheus, internal) |
| `agent` | Python LiveKit agent worker pool | 9090 (Prometheus), 8081 (internal) |
| `token-server` | LiveKit JWT issuer | 8080 |
| `redis` | LiveKit state store | 6379 (internal) |
| `demo-frontend` | Optional Next.js demo UI (`--profile demo`) | 3000 |
| `prometheus` | Metrics scraper (`--profile observability`) | 9091 |
| `grafana` | Dashboards (`--profile observability`) | 3001 |

Langfuse runs as a separate compose project under `observability/langfuse/` — see [observability.md](observability.md).

## External Services (not in this repo)

| Service | URL (default) | Protocol |
|---|---|---|
| ASR | `http://host.docker.internal:8102` | `POST /api/transcribe/` multipart WAV |
| TTS (wrapper) | `http://host.docker.internal:8000` | `POST /` JSON `{text}` → WAV |
| Nusuk LLM | `https://dev.nusukai.com` | `POST /chat/stream` SSE |

## Key Design Decisions

- Turn detection is **always on** (`MultilingualModel` when installed, VAD-only fallback)
- TTS input is stripped of markdown before synthesis (LLM responses contain `**bold**`, `[1]` citations)
- Nusuk does not accept a system prompt; a `CUSTOM_LLM_QUERY_PREFIX` is prepended to every user query instead
- Room I/O defaults (16 kHz mono, 50 ms frames, pre-connect audio) are **hard-coded** in `agent.py` — not env-configurable
- All three HTTP clients (`STT`, `LLM`, `TTS`) are closed on session teardown regardless of error path

## Related Docs

- [diagrams/](diagrams/) — Excalidraw system diagrams (canonical visual)
- [architecture.md](architecture.md) — component diagram and data flow
- [agents.md](agents.md) — agent session lifecycle and behavior
- [livekit.md](livekit.md) — LiveKit SDK patterns used here
- [functions.md](functions.md) — internal function reference
- [workflows.md](workflows.md) — end-to-end execution paths
- [troubleshooting.md](troubleshooting.md) — known issues and fixes
- [changelog.md](changelog.md) — ongoing updates and decisions log
