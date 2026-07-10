"""Small helpers shared by desktop pages."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar, cast

from PyQt6.QtWidgets import QWidget

from app.services.context import services_for
from app.tasking import WorkerFailure


BackendT = TypeVar('BackendT')


def error_text(error: object) -> str:
    """Return a concise message while retaining worker tracebacks elsewhere."""

    if isinstance(error, WorkerFailure):
        return error.message or type(error.exception).__name__
    return str(error)


def resolve_backend_provider(
    owner: QWidget,
    fallback: Callable[[], BackendT],
) -> Callable[[], BackendT]:
    """Capture a backend factory on the GUI thread before workers start."""

    services = services_for(owner)
    if services is None:
        return fallback
    return cast(Callable[[], BackendT], services.backend_provider)


__all__ = ['error_text', 'resolve_backend_provider']
