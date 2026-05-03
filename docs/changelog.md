# Changelog

Ongoing record of significant changes, decisions, and findings. Most recent first.

---

## 2026-05-03

### Removed Langfuse — Prometheus + Grafana only
Removed `langfuse>=2,<4` from `agent/requirements.txt`, deleted `agent/observability.py`, removed `LangfuseSettings` from `agent/config.py`, dropped all `import observability` / `start_span` / `start_generation` calls from `custom_stt.py`, `custom_llm.py`, `custom_tts.py`, and `agent.py`. Removed `LANGFUSE_*` vars from `.env.example` and `.env`. Removed `observability/langfuse/` stack. Prometheus + Grafana remain under `--profile observability`. Updated all docs.

---

## 2026-04-22 (observability stack)

### Added Prometheus + Grafana compose services
New `prometheus` and `grafana` services in [docker-compose.yml](../docker-compose.yml) under profile `observability` (`docker compose --profile observability up -d`). Prometheus scrape config at [observability/prometheus.yml](../observability/prometheus.yml) covers `agent:9090` (existing histograms) and `livekit-server:6789` (new — enabled via `prometheus_port: 6789` in [livekit-server/livekit.yaml](../livekit-server/livekit.yaml)). Grafana auto-provisions a Prometheus datasource and a starter dashboard **S2S / S2S Agent** from [observability/grafana/provisioning/](../observability/grafana/provisioning/) — panels: active sessions, STT/LLM/TTS p50/p95/p99 latency, error rates. Default host ports: Grafana 3001, Prometheus 9091 (both overridable).

### Added Langfuse trace instrumentation to plugins
New module [agent/observability.py](../agent/observability.py) — per-worker Langfuse client (`init`), per-session contextvar (`set_session`), and `start_span` / `start_generation` helpers returning live spans or `_NoOpSpan`s when disabled. [agent/plugins/custom_stt.py](../agent/plugins/custom_stt.py) wraps its HTTP call in an `stt` span, [agent/plugins/custom_llm.py](../agent/plugins/custom_llm.py) wraps both openai and nusuk stream paths in an `llm-chat` generation (with `ttft_s` / `duration_s` metadata), and [agent/plugins/custom_tts.py](../agent/plugins/custom_tts.py) wraps its HTTP call in a `tts` span. All three pass `session_id = LiveKit room name` and `user_id = participant identity` via `update_trace` so Langfuse's Sessions view groups a whole call into a waterfall. `LangfuseSettings` added to [agent/config.py](../agent/config.py); new env vars `LANGFUSE_ENABLED`, `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_FLUSH_AT`, `LANGFUSE_FLUSH_INTERVAL`.

### Self-hosted Langfuse v3 stack
New [observability/langfuse/docker-compose.yml](../observability/langfuse/docker-compose.yml) — separate compose project for isolation, services: `langfuse-web`, `langfuse-worker`, `postgres`, `clickhouse`, `redis`, `minio`. Port remaps from upstream defaults to avoid conflicts: web 3000 → 3100 (demo-frontend owns 3000), MinIO API 9000 → 9190, MinIO console 9001 → 9191 (Prometheus owns 9091), Postgres → 5532, Redis → 6389, ClickHouse native → 9500. Secrets and `LANGFUSE_INIT_*` bootstrap vars live in the project-local `.env` (gitignored). Agent reaches Langfuse via `host.docker.internal:3100` (same pattern as ASR/TTS).

### Fixed `prewarm()` never running
Reverted `prewarm()` from `async def` back to sync. The livekit-agents 1.5.x SDK invokes `setup_fnc` from a sync context — an `async def prewarm` produced a coroutine that was never awaited, so metrics.start_server, VAD load, and (previously) Nusuk token prefetch were silently dropped. Sync prewarm now actually runs. JWT prefetch wrapped in `asyncio.run(token_manager.get_token())` to preserve the prewarm-time fetch without needing an async function. This resolves a previously-silent bug introduced in the 2026-04-20 prewarm change.

### Narrowed LiveKit UDP port range back to 50000–50100
Reverted the 2026-04-20 widening to 60000. The 10k-port Docker bind races against ephemeral UDP sockets on busy hosts and fails with `address already in use`. The running container since 2026-04-15 had been on the narrow 50000–50100 range and worked fine, so the narrower range was restored to unblock restarts. For >50 concurrent participants in production, switch `livekit-server` to `network_mode: host` (no Docker port proxy) rather than widening the mapped range.

### Added `COPY` for metrics.py and observability.py in agent Dockerfile
[agent/Dockerfile](../agent/Dockerfile) was missing `COPY metrics.py` (since 2026-04-20) — the build-time `download-files` step failed with `ModuleNotFoundError: No module named 'metrics'`. Runtime worked only because `docker-compose.yml` volume-mounts `./agent:/app` at runtime, masking the incomplete image. Now both `metrics.py` and the new `observability.py` are explicitly copied.

### New doc
[docs/observability.md](observability.md) — full setup + ports reference for both the Prometheus/Grafana and Langfuse stacks.

---

## 2026-04-20 (audio pipeline cleanup)

### Agent input sample rate lowered from 24 kHz to 16 kHz
`AudioInputOptions.sample_rate` in [agent.py:93](../agent/agent.py#L93) changed from 24000 → 16000. LiveKit's server now delivers 16 kHz audio directly — matching Silero VAD's native rate and the ASR target rate. The `rtc.AudioResampler` call in `custom_stt.py` becomes a no-op (guarded by `if sample_rate != target_sample_rate`) and is kept as a safety net. Saves one resample per turn and avoids mild filter ringing from the 48→24→16 chain. TTS output rate is unchanged (24 kHz, independent stream).

---

## 2026-04-20 (concurrency + observability)

### Added load function to AgentServer
`server.load_threshold = 0.8` and `server.load_fnc = lambda s: min(len(s.active_jobs) / _MAX_JOBS_PER_WORKER, 1.0)`. Workers stop accepting new rooms above 80% of their job cap. Cap defaults to 10, overridable via `AGENT_MAX_JOBS_PER_WORKER`. Previously there was no cap — workers would accept unlimited jobs until OOM.

### Prefetch Nusuk token in prewarm
`prewarm()` is now `async`. When `CUSTOM_LLM_PROVIDER=nusuk` with `client_id/secret` set, it pre-fetches the JWT into `proc.userdata["nusuk_token_manager"]`. `entrypoint` passes this to `CustomLLM` via the new `token_manager=` parameter. The token manager is shared across all sessions on the same worker process — first room no longer pays an auth roundtrip before its first LLM call. `CustomLLM.__init__` falls back to creating its own session-scoped manager if no pre-warmed one is available.

### Added Prometheus metrics
New `agent/metrics.py` with counters, gauges, and histograms for: active sessions, STT duration/errors, LLM TTFT / total duration / errors (labelled by provider), TTS duration/errors. Metrics server starts in `prewarm()` on `AGENT_METRICS_PORT` (default 9090). Agent container now exposes that port via docker-compose. Access at `http://localhost:9090/metrics`. Note: for multi-worker-process containers, configure `PROMETHEUS_MULTIPROC_DIR` for cross-process aggregation.

### Widened LiveKit UDP port range
`livekit.yaml` `port_range_end` and docker-compose UDP mapping changed from 50100 to 60000 (10k ports → ~2500 concurrent participant slots instead of ~50).

---

## 2026-04-20 (docs)

### Added `docs/` folder
Created long-term institutional memory for the agent system:
- `docs/overview.md` — system summary, demos, services, design decisions
- `docs/architecture.md` — ASCII component diagram, data flow, machine split
- `docs/agents.md` — startup sequence, session parameters, Nusuk behavior
- `docs/livekit.md` — LiveKit SDK patterns, custom adapter contracts
- `docs/functions.md` — internal function reference for all Python modules
- `docs/workflows.md` — end-to-end execution paths for all major flows
- `docs/troubleshooting.md` — known issues, fixes, and debugging steps

### Added `CUSTOM_LLM_QUERY_PREFIX` support
Nusuk ignores `system_prompt`. A bilingual query prefix is now prepended to every user query to control response style (short sentences, proper punctuation, no markdown). Set via `CUSTOM_LLM_QUERY_PREFIX` env var. Wired in both the Python agent and the PTT demo frontend.

### Fixed sentence buffering not firing
Root cause: Nusuk was returning 150+ word responses with no punctuation, so `AgentSession`'s sentence boundary detection never triggered. Fix: query prefix instructs Nusuk to use short sentences ending with `.` or `،`.

### Added markdown stripping to TTS layer
`_strip_markdown()` in `custom_tts.py` removes `**bold**`, `*italic*`, `> blockquotes`, `[4]` citation markers, and `\n\n` paragraph breaks before posting to the TTS service. Prevents the TTS from speaking formatting symbols.

### Added markdown stripping to PTT TTS route
`stripMarkdown()` added to `demo/app/api/ptt/tts/route.ts` for parity with the LiveKit agent behavior.

### Added `NUSUK_QUERY_PREFIX` to PTT chat route
PTT chat route now reads `NUSUK_QUERY_PREFIX` env var and prepends it to user queries, matching agent behavior.

### Removed VAD toggle from token route
Deleted all `turnDetection` code from `demo/app/api/token/route.ts` (variable declaration, body parsing, query string parsing, `roomMetadata`/`roomConfig.metadata` assignment). Turn detection is always on; the toggle was never needed. `MultilingualModel` is used when installed; VAD-only fallback otherwise.

### Python code cleanup (all plugin files)
- Extracted shared `_iter_sse()` SSE parser used by both `_run_openai` and `_run_nusuk`
- Extracted `_extract_openai_delta()` helper
- Replaced `while True + tried_refresh` retry with `for attempt in range(2):`
- Normalized `_provider_key` in `__init__` for both LLM and STT adapters
- Changed `conn_options` parameter in STT to `conn_options: Any = None  # noqa: ARG002` (must not be renamed — SDK uses it as keyword arg)
- Replaced STT `or`-chain text extraction with explicit `for key in (...)` loop
- Hardened `nusuk_auth.py`: `assert` → explicit check, `except Exception` → specific types, `3600.0` → `_DEFAULT_TOKEN_TTL`, JWT split length guard
- WAV detection: `if settings.audio_format == "wav" or audio_bytes[:4] == b"RIFF"` → `if audio_bytes[:4] == b"RIFF"` (magic bytes more robust)
- `_tts_url` wrapper branch dead code removed
- Added inline comments to all `AgentSession` and `RoomOptions` parameters

### Added `query_prefix` field to `LLMSettings`
New `CUSTOM_LLM_QUERY_PREFIX` env var. Stored in `LLMSettings.query_prefix`, prepended to every user query in `_run_nusuk()`.

### Fixed eval comparison to be fair
`eval/compare.py` was comparing Groq LLM + `local_api` TTS (direct mode) against Nusuk LLM + `wrapper` TTS (LiveKit mode). Added Nusuk provider support and `wrapper` TTS support to `direct_llm()` and `direct_tts()` so both modes use the same providers. Also added `eval/requirements.txt`.

---

## 2026-04-18 (approx)

### Built push-to-talk (PTT) demo
New `/ptt` route in the Next.js demo with hold-to-talk button, sequential ASR → Nusuk chat → TTS pipeline, status chips per stage, and server-side Nusuk auth via `demo/lib/nusukAuth.ts`.

New API proxy routes:
- `demo/app/api/ptt/transcribe/route.ts` — proxy to ASR service
- `demo/app/api/ptt/chat/route.ts` — proxy to Nusuk `/chat` (non-streaming, server-side auth)
- `demo/app/api/ptt/tts/route.ts` — proxy to TTS wrapper service

### Fixed LiveKit TTS adapter for F5-TTS wrapper
Nusuk's TTS wrapper (`provider=wrapper`) expects `POST /` with `{"text": "..."}` — no auth, no path suffix. Added `wrapper` provider to `_tts_url()` and `_request_payload()`.

### Wired Nusuk automatic auth in Python agent
`NusukTokenManager` created in `CustomLLM.__init__` when `CUSTOM_LLM_CLIENT_ID` + `CUSTOM_LLM_CLIENT_SECRET` are set. Tokens refreshed automatically using JWT `exp` claim. On 401, token invalidated and one retry issued.

### Fixed agent to use Nusuk for LLM
Updated `.env`: `CUSTOM_LLM_PROVIDER=nusuk`, `CUSTOM_LLM_URL=https://dev.nusukai.com`. STT URL updated to `http://host.docker.internal:8102`.

### Added error resilience to STT and TTS adapters
Both adapters now catch `httpx.HTTPError`, log the error, and return gracefully (empty transcript / empty audio) so the session survives service failures.

### Added LiveKit healthcheck
`livekit-server` service in `docker-compose.yml` has a `curl` healthcheck. Agent and token-server `depends_on` with `condition: service_healthy` so they don't register before the server is ready.

### Rewrote agent.py
- Added `_AGENT_PARTICIPANT_KIND = 4` constant
- Added `_aclose_providers()` helper (was duplicated)
- Always-on turn detection (removed `use_turn_detector` toggle logic — `MultilingualModel` when installed, VAD fallback otherwise)
- Added inline comments to all `AgentSession` and `RoomOptions` parameters
- Added explicit EOS mode (`AGENT_EXPLICIT_EOS_MODE=true`) for eval
- Added stage logging: `stage=session_start`, `stage=session_ready`

### Added production deployment docs
README updated with:
- CPU/GPU machine split guidance
- LiveKit public IP requirement (most common production failure)
- VAD: CPU-only, no GPU benefit
- Horizontal scaling table (agent replicas, ASR replicas, TTS replicas, LiveKit server)

---

## Design decisions on record

### Nusuk `system_prompt` is ignored
Nusuk does not honor the `system_prompt` field. Response style is controlled by prepending a query prefix to every user message. This is a workaround, not a feature. If Nusuk adds system prompt support, remove `CUSTOM_LLM_QUERY_PREFIX` and use `AGENT_SYSTEM_PROMPT` directly.

### Room I/O defaults are hard-coded
Audio input (24 kHz mono, 50 ms frames, pre-connect audio) is hard-coded in `agent.py` rather than exposed as env vars. These values are stable and don't need per-deployment tuning. Changing them requires a code edit and image rebuild.

### VAD is always on
Silero VAD is preloaded in `prewarm()` and passed to both `stt.StreamAdapter` and `AgentSession`. There is no env var to disable it — disabling VAD would break the streaming STT interface the SDK expects. Turn *detection* (when to commit a turn) is separate from VAD (when speech is present).

### `MultilingualModel` does not support Arabic
`MultilingualModel` (LiveKit `turn_detector` plugin) improves turn detection for supported languages but falls back to VAD-only for Arabic. The agent logs a warning but continues. VAD-only fallback adds 0.5–3 s overhead depending on silence length.

### Session history is in memory only
`AgentSession` keeps conversation history in memory for the lifetime of the room. It is cleared when the room ends. No persistence layer is included. Add one only if cross-session history or analytics are needed.

### `conn_options` parameter name is load-bearing
The LiveKit SDK calls `_recognize_impl(buffer, conn_options=...)` as a keyword argument. The parameter must be named exactly `conn_options` even though it is unused. Renaming it to `_conn_options` or deleting it causes a `TypeError` at runtime that produces silent failures (empty transcripts) without a visible traceback in some log configurations.
