from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import UUID

_event_run_id: ContextVar[UUID | None] = ContextVar("amk_event_run_id", default=None)


def event_run_id(fallback: UUID) -> UUID:
    return _event_run_id.get() or fallback


@contextmanager
def bind_event_run_id(run_id: UUID) -> Iterator[None]:
    token = _event_run_id.set(run_id)
    try:
        yield
    finally:
        _event_run_id.reset(token)
