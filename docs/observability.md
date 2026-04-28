# Observability

Two independent stacks, each opt-in:

- **Prometheus + Grafana** — infra/latency metrics (agent, LiveKit, Triton)
- **Langfuse** — session/turn traces (STT, LLM, TTS content, waterfall)

Both live under `observability/`. Neither is in the default `make up`.

## Stack 1 — Prometheus + Grafana

### What it measures

The agent already exports `agent_*` metrics via `prometheus_client` (see `agent/metrics.py`). LiveKit exports room/participant/bandwidth counters when `prometheus_port` is set.

| Metric prefix | Source | Exporter port |
|---|---|---|
| `agent_active_sessions_total`, `agent_stt_*`, `agent_llm_*`, `agent_tts_*` | `agent/metrics.py` | `:9090` |
| `livekit_*` (rooms, participants, bandwidth) | LiveKit server (`prometheus_port: 6789` in `livekit-server/livekit.yaml`) | `:6789` |

### Bringing it up

```
docker compose --profile observability up -d prometheus grafana
```

Scraping is driven by `observability/prometheus.yml`. Grafana auto-provisions a Prometheus datasource and a starter dashboard **S2S / S2S Agent** from `observability/grafana/provisioning/`.

### URLs

- Prometheus: `http://localhost:${PROMETHEUS_PORT:-9091}`
- Grafana: `http://localhost:${GRAFANA_PORT:-3001}` — admin / `${GRAFANA_ADMIN_PASSWORD:-admin}`

### Adding panels

Either edit in the UI and export to `observability/grafana/provisioning/dashboards/agent.json`, or add a new JSON file next to it — the provider auto-reloads every 30s.

### Gotchas

- Agent + LiveKit containers must be started with the current compose file (port binding) and current `livekit.yaml` (for `prometheus_port`). Existing containers from older versions won't expose the metrics endpoints — recreate with `docker compose up -d --no-deps agent livekit-server`.
- Prometheus scrapes inside the `s2s-orchestrator_default` network by service name (`agent:9090`, `livekit-server:6789`). No host ports need to be exposed for scraping to work.

## Stack 2 — Langfuse

### What it captures

One Langfuse trace per STT call, LLM generation, and TTS call. All traces tagged with `session_id = LiveKit room name` and `user_id = participant identity`, so Langfuse's **Sessions** view groups a whole call into a waterfall.

| Plugin | Observation type | Name | Input | Output |
|---|---|---|---|---|
| `plugins/custom_stt.py` | span | `stt` | provider, frame count, audio bytes | transcript text, request_id |
| `plugins/custom_llm.py` | generation | `llm-chat` | query/messages, model | assistant text, ttft_s, duration_s |
| `plugins/custom_tts.py` | span | `tts` | provider, text | request_id, audio bytes, sample rate, duration_s |

Wiring lives in `agent/observability.py` (client init + `set_session` + `start_span` / `start_generation` helpers). Plugins guard on the helpers returning a no-op when disabled, so runtime cost is one `contextvar.get()` when `LANGFUSE_ENABLED=false`.

### Stack layout

The Langfuse stack (web + worker + Postgres + ClickHouse + Redis + MinIO) lives in `observability/langfuse/` as a **separate compose project** with its own lifecycle. It talks to the agent via `host.docker.internal` (same pattern as ASR/TTS).

### Bringing it up

```
cd observability/langfuse
cp .env.example .env       # edit secrets (openssl rand -hex 32 for each)
docker compose up -d
```

UI at `http://localhost:${LANGFUSE_WEB_PORT:-3100}`.

The `.env.example` includes `LANGFUSE_INIT_*` variables — if set on a **fresh** boot, Langfuse auto-creates an org, project, user, and API keys so you can start sending traces without clicking through setup.

### Wiring the agent to Langfuse

Set in the main `./.env` at the project root:

```
LANGFUSE_ENABLED=true
LANGFUSE_HOST=http://host.docker.internal:3100
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Restart the agent: `docker compose up -d --no-deps agent`.

### What to look at in the UI

- **Sessions** — one row per LiveKit room. Click to see all STT/LLM/TTS observations in timeline order.
- **Traces** — one row per individual observation (useful for filtering by model or latency).
- **Generations** — LLM-only view with token usage and cost if the provider reports it.

### Gotchas

- Langfuse v3 requires `ENCRYPTION_KEY` to be **exactly 64 hex chars** (`openssl rand -hex 32`).
- MinIO API bound to host `:9190` (not `:9090`) so the default Prometheus port stays free.
- The agent container reaches Langfuse via `host.docker.internal`. This works because `docker-compose.yml` has `extra_hosts: ["host.docker.internal:host-gateway"]` on the `agent` service.
- Enabling Langfuse with an unreachable host will log warnings but will not break the agent. The SDK buffers silently and drops on shutdown.

## Ports reference

Forward these from your laptop (VSCode Ports panel or `ssh -L`) to browse the stacks locally.

### Browser-facing (need forwarding if remote)

| Host port | Service | Purpose |
|---|---|---|
| 3000 | demo-frontend | Browser client that talks to the agent |
| 3001 | Grafana | Metrics dashboards (admin / `${GRAFANA_ADMIN_PASSWORD}`) |
| 3100 | Langfuse web | Session / trace browser |
| 9091 | Prometheus UI | Direct metric queries, scrape target health |
| 9190 | MinIO API | Langfuse signed media URLs follow this |
| 9191 | MinIO console | MinIO admin (rarely needed) |
| 8080 | token-server | LiveKit JWT issuer the demo-frontend calls |

### LiveKit WebRTC (required for a real call)

| Host port | Protocol | Purpose |
|---|---|---|
| 7880 | TCP/WS | WebRTC signaling |
| 7881 | TCP | Media fallback when UDP is blocked |
| 50000–50100 | UDP | WebRTC audio media (see `docker-compose.yml` for the width rationale) |

### Internal-only (never touched from a browser)

Prometheus and Langfuse-worker reach these by Docker service name; the main stack does not expose them to the host.

| Port | Service | Consumer |
|---|---|---|
| 9090 inside agent | Prometheus `/metrics` | `prometheus` scrape |
| 6789 inside livekit-server | Prometheus `/metrics` | `prometheus` scrape |
| 8081 inside agent | livekit-agents HTTP status | Docker healthcheck |
| 9000 inside minio | MinIO internal API | `langfuse-worker` |
| 8123 / 9500 inside clickhouse | ClickHouse HTTP / native | `langfuse-worker` |
| 6389 → 6379 inside Langfuse redis | Langfuse job queue | `langfuse-worker` |
| 5532 → 5432 inside Langfuse postgres | Langfuse main DB | `langfuse-web` / `langfuse-worker` |
| 3030 inside langfuse-worker | Langfuse worker metrics | Reserved — not scraped yet |

## See also

- [architecture.md](architecture.md) — where STT/LLM/TTS HTTP calls happen
- [agents.md](agents.md) — `prewarm()` and the session lifecycle (metrics init + Langfuse init both run in prewarm)
- `agent/metrics.py` — Prometheus metric definitions
- `agent/observability.py` — Langfuse helpers
