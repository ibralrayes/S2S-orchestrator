from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass

from config import LangfuseSettings

logger = logging.getLogger("nusuk-agent.observability")

try:
    from langfuse import Langfuse
except ImportError:  # pragma: no cover
    Langfuse = None  # type: ignore[assignment,misc]


@dataclass(slots=True, frozen=True)
class SessionInfo:
    session_id: str
    user_id: str | None


_client: "Langfuse | None" = None
_current_session: ContextVar[SessionInfo | None] = ContextVar(
    "langfuse_current_session", default=None
)


def init(settings: LangfuseSettings) -> None:
    global _client
    if _client is not None:
        return
    if not settings.enabled:
        logger.info("observability disabled")
        return
    if Langfuse is None:
        logger.warning("langfuse package not installed; observability disabled")
        return
    if not settings.public_key or not settings.secret_key:
        logger.warning("langfuse keys missing; observability disabled")
        return

    _client = Langfuse(
        public_key=settings.public_key,
        secret_key=settings.secret_key,
        host=settings.host,
        flush_at=settings.flush_at,
        flush_interval=settings.flush_interval,
    )
    logger.info("observability enabled host=%s", settings.host)


def get_client() -> "Langfuse | None":
    return _client


def current_session() -> SessionInfo | None:
    return _current_session.get()


def set_session(session_id: str, user_id: str | None) -> None:
    _current_session.set(SessionInfo(session_id=session_id, user_id=user_id))


class _NoOpSpan:
    def update(self, **_: object) -> None:
        pass

    def end(self, **_: object) -> None:
        pass


def start_span(name: str, *, input: object | None = None) -> object:
    """Start a Langfuse span tagged with the current session, or return a no-op."""
    if _client is None:
        return _NoOpSpan()
    session = _current_session.get()
    if session is None:
        return _NoOpSpan()
    span = _client.start_span(name=name, input=input)
    span.update_trace(session_id=session.session_id, user_id=session.user_id)
    return span


def start_generation(
    name: str,
    *,
    model: str | None = None,
    input: object | None = None,
) -> object:
    """Start a Langfuse LLM generation tagged with the current session, or return a no-op."""
    if _client is None:
        return _NoOpSpan()
    session = _current_session.get()
    if session is None:
        return _NoOpSpan()
    gen = _client.start_generation(name=name, model=model, input=input)
    gen.update_trace(session_id=session.session_id, user_id=session.user_id)
    return gen


def shutdown() -> None:
    global _client
    if _client is None:
        return
    try:
        _client.flush()
    except Exception:
        logger.warning("langfuse flush failed", exc_info=True)
    _client = None
