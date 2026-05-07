from __future__ import annotations

import logging
import os

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    multiprocess,
    start_http_server,
)

logger = logging.getLogger("nusuk-agent.metrics")

# ── Active sessions ────────────────────────────────────────────────────────────
# `multiprocess_mode="livesum"` aggregates only across live worker processes so
# the gauge reflects current active sessions across all forks.
ACTIVE_SESSIONS = Gauge(
    "agent_active_sessions_total",
    "Currently active agent sessions",
    multiprocess_mode="livesum",
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

# ── Per-turn pipeline latencies ───────────────────────────────────────────────
# Populated at session end from `session.history` — the SDK records these on
# `ChatMessage.metrics` (MetricsReport) as turns happen. Mirrors LiveKit's OTel
# histograms (`lk.agents.turn.*`) but routed through our multiproc-safe Prom
# registry so all workers' samples aggregate correctly.
TURN_E2E_LATENCY = Histogram(
    "agent_turn_e2e_latency_seconds",
    "Time from end of user speech to first agent response (assistant turn)",
    buckets=[0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0],
)
TURN_LLM_TTFT = Histogram(
    "agent_turn_llm_node_ttft_seconds",
    "LLM node time-to-first-token (assistant turn, post turn-confirmation)",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)
TURN_TTS_TTFB = Histogram(
    "agent_turn_tts_node_ttfb_seconds",
    "TTS node time-to-first-byte after first text token (assistant turn)",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0],
)
TURN_TRANSCRIPTION_DELAY = Histogram(
    "agent_turn_transcription_delay_seconds",
    "Time from end of user speech to final transcript available (user turn)",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0],
)
TURN_END_OF_TURN_DELAY = Histogram(
    "agent_turn_end_of_turn_delay_seconds",
    "Time from end of user speech to turn-end decision (user turn)",
    buckets=[0.1, 0.25, 0.5, 1.0, 2.0, 3.0, 5.0],
)


def record_turn_metrics(history) -> None:
    """Walk a `livekit.agents.llm.ChatContext` and observe per-turn metrics.

    Call once at session end. Each `ChatMessage.metrics` field is populated by
    the SDK as turns happen; user messages carry transcription/end_of_turn,
    assistant messages carry e2e_latency/llm_ttft/tts_ttfb.
    """
    try:
        messages = history.messages()
    except Exception:
        return
    for msg in messages:
        m = getattr(msg, "metrics", None) or {}
        v = m.get("transcription_delay")
        if v is not None:
            TURN_TRANSCRIPTION_DELAY.observe(v)
        v = m.get("end_of_turn_delay")
        if v is not None:
            TURN_END_OF_TURN_DELAY.observe(v)
        v = m.get("e2e_latency")
        if v is not None:
            TURN_E2E_LATENCY.observe(v)
        v = m.get("llm_node_ttft")
        if v is not None:
            TURN_LLM_TTFT.observe(v)
        v = m.get("tts_node_ttfb")
        if v is not None:
            TURN_TTS_TTFB.observe(v)


def start_server(port: int) -> None:
    """Start the Prometheus HTTP server on the given port.

    Safe to call in forked worker processes. When PROMETHEUS_MULTIPROC_DIR is
    set, the bound worker serves a registry backed by `MultiProcessCollector`,
    which aggregates counter/histogram/gauge values written by every worker
    into shared memory-mapped files. Without it, only the worker that won the
    bind race exports its in-process counters and the rest are silently lost.
    """
    multiproc_dir = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    registry = None
    if multiproc_dir:
        os.makedirs(multiproc_dir, exist_ok=True)
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    try:
        start_http_server(port, registry=registry) if registry else start_http_server(port)
        logger.info(
            "metrics_server_started port=%d multiproc=%s",
            port,
            bool(multiproc_dir),
        )
    except OSError:
        # Another worker in this container already bound the port. With
        # multiproc enabled, the bound worker still aggregates everyone's
        # samples — silent skip is correct.
        pass
