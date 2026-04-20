# Changelog

Ongoing record of significant changes, decisions, and findings. Most recent first.

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
