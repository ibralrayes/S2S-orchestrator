# Observability

Metrics collected via **Prometheus + Grafana** under the `observability` compose profile.

Both services live in the main `docker-compose.yml` — no separate project needed.

## What it measures

The agent exports `agent_*` metrics via `prometheus_client` (see `agent/metrics.py`). LiveKit exports room/participant/bandwidth counters when `prometheus_port` is set.

| Metric prefix | Source | Exporter port |
|---|---|---|
| `agent_active_sessions_total`, `agent_stt_*`, `agent_llm_*`, `agent_tts_*` | `agent/metrics.py` (per-stage HTTP wall time, recorded at request boundaries) | `:9090` |
| `agent_turn_*` (e2e_latency, llm_node_ttft, tts_node_ttfb, transcription_delay, end_of_turn_delay) | `metrics.record_turn_metrics(session.history)` — invoked at session end; reads `ChatMessage.metrics` populated natively by the LiveKit SDK | `:9090` |
| `livekit_*` (rooms, participants, bandwidth) | LiveKit server (`prometheus_port: 6789` in `livekit-server/livekit.yaml`) | `:6789` |

### `agent_*` vs `agent_turn_*`

Two complementary views:

- **`agent_stt_*` / `agent_llm_*` / `agent_tts_*`** — measured by *us*, per HTTP request, observed inline. Captures wire-level wall time and HTTP errors. Useful for vendor-side debugging.
- **`agent_turn_*`** — measured by the *SDK*, per turn, walked at session end. Same data the SDK exports through OpenTelemetry (`lk.agents.turn.*`), but routed through our multi-process Prometheus registry so all forked workers contribute. Useful for end-to-end user-perceived latency. `e2e_latency` is the headline number for "how fast does the agent reply after the user stops talking."

### Why we did not adopt the OTel→Prometheus bridge

LiveKit ships `livekit.agents.telemetry.otel_metrics` with pre-built OTel histograms. Wiring those through `opentelemetry-exporter-prometheus` would only emit data from the worker that wins the port-9090 bind race; the other forked workers' metrics would be silently dropped (the OTel SDK is not multi-process-aware in the way `prometheus_client` is). We therefore skipped the OTel SDK and instead read the same SDK-populated `MetricsReport` data into multiproc-safe Prometheus histograms with matching names.

## Bringing it up

```
docker compose --profile observability up -d prometheus grafana
```

Scraping is driven by `observability/prometheus.yml`. Grafana auto-provisions a Prometheus datasource and a starter dashboard **S2S / S2S Agent** from `observability/grafana/provisioning/`.

## URLs

- Prometheus: `http://localhost:${PROMETHEUS_PORT:-9091}`
- Grafana: `http://localhost:${GRAFANA_PORT:-3001}` — admin / `${GRAFANA_ADMIN_PASSWORD:-admin}`

## Adding panels

Either edit in the UI and export to `observability/grafana/provisioning/dashboards/agent.json`, or add a new JSON file next to it — the provider auto-reloads every 30 s.

## Gotchas

- Agent + LiveKit containers must be started with the current compose file (port binding) and current `livekit.yaml` (for `prometheus_port`). Existing containers from older versions won't expose the metrics endpoints — recreate with `docker compose up -d --no-deps agent livekit-server`.
- Prometheus scrapes inside the `s2s-orchestrator_default` network by service name (`agent:9090`, `livekit-server:6789`). No host ports need to be exposed for scraping to work.

## Ports reference

Forward these from your laptop (VSCode Ports panel or `ssh -L`) to browse the stacks locally.

### Browser-facing (need forwarding if remote)

| Host port | Service | Purpose |
|---|---|---|
| 3000 | demo-frontend | Browser client that talks to the agent |
| 3001 | Grafana | Metrics dashboards (admin / `${GRAFANA_ADMIN_PASSWORD}`) |
| 9091 | Prometheus UI | Direct metric queries, scrape target health |
| 8080 | token-server | LiveKit JWT issuer the demo-frontend calls |

### LiveKit WebRTC (required for a real call)

| Host port | Protocol | Purpose |
|---|---|---|
| 7880 | TCP/WS | WebRTC signaling |
| 7881 | TCP | Media fallback when UDP is blocked |
| 50000–50100 | UDP | WebRTC audio media (see `docker-compose.yml` for the width rationale) |

### Internal-only (never touched from a browser)

Prometheus reaches these by Docker service name; the main stack does not expose them to the host.

| Port | Service | Consumer |
|---|---|---|
| 9090 inside agent | Prometheus `/metrics` | `prometheus` scrape |
| 6789 inside livekit-server | Prometheus `/metrics` | `prometheus` scrape |
| 8081 inside agent | livekit-agents HTTP status | Docker healthcheck |

## See also

- [architecture.md](architecture.md) — where STT/LLM/TTS HTTP calls happen
- [agents.md](agents.md) — `prewarm()` and the session lifecycle
- `agent/metrics.py` — Prometheus metric definitions
