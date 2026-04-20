from __future__ import annotations

import logging

from prometheus_client import Counter, Gauge, Histogram, start_http_server

logger = logging.getLogger("nusuk-agent.metrics")

# ── Active sessions ────────────────────────────────────────────────────────────
ACTIVE_SESSIONS = Gauge(
    "agent_active_sessions_total",
    "Currently active agent sessions",
)

# ── STT ───────────────────────────────────────────────────────────────────────
STT_DURATION = Histogram(
    "agent_stt_duration_seconds",
    "STT HTTP request wall time",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)
STT_ERRORS = Counter(
    "agent_stt_errors_total",
    "STT request failures (HTTP or parse errors)",
)

# ── LLM ───────────────────────────────────────────────────────────────────────
LLM_TTFT = Histogram(
    "agent_llm_ttft_seconds",
    "LLM time-to-first-token (from request to first streamed delta)",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)
LLM_DURATION = Histogram(
    "agent_llm_duration_seconds",
    "LLM total stream duration (request to last token)",
    buckets=[0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)
LLM_ERRORS = Counter(
    "agent_llm_errors_total",
    "LLM request failures",
    ["provider"],
)

# ── TTS ───────────────────────────────────────────────────────────────────────
TTS_DURATION = Histogram(
    "agent_tts_duration_seconds",
    "TTS synthesis wall time (request to last audio byte)",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)
TTS_ERRORS = Counter(
    "agent_tts_errors_total",
    "TTS request failures",
)


def start_server(port: int) -> None:
    """Start the Prometheus HTTP server on the given port.

    Safe to call in forked worker processes — the second caller silently skips
    binding if the port is already occupied by the first worker.
    For production multi-process setups, configure PROMETHEUS_MULTIPROC_DIR and
    use prometheus_client.multiprocess.MultiProcessCollector instead.
    """
    try:
        start_http_server(port)
        logger.info("metrics_server_started port=%d", port)
    except OSError:
        # Another worker process in this container already bound the port.
        pass
