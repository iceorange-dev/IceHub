"""Helpers for starting UI workers through the application service boundary."""

from __future__ import annotations

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QWidget

from app.tasking import WorkerRegistry

from .context import services_for


_STANDALONE_REGISTRY_ATTRIBUTE = '_icehub_worker_registry'


def _standalone_registry(owner: QWidget) -> WorkerRegistry:
    """Return one owner-attached registry for a page outside ``MainWindow``."""

    registry = getattr(owner, _STANDALONE_REGISTRY_ATTRIBUTE, None)
    if isinstance(registry, WorkerRegistry):
        return registry
    registry = WorkerRegistry(owner)
    setattr(owner, _STANDALONE_REGISTRY_ATTRIBUTE, registry)
    return registry


def start_registered_worker(
    owner: QWidget,
    worker: QThread,
    resources: str | list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Start a strongly-owned worker after atomically leasing resources."""

    services = services_for(owner)
    token = None
    if resources and services is not None:
        token = services.operation_coordinator.acquire(resources, owner=owner)
        if token is None:
            return False
        worker.finished.connect(token.release)

    try:
        if services is not None:
            services.worker_registry.start(worker)
        else:
            # Standalone integrations still need a strong reference until the
            # thread emits finished; a bare start can let Python destroy it.
            _standalone_registry(owner).start(worker)
    except BaseException:
        if token is not None:
            token.release()
        raise
    return True


__all__ = ['start_registered_worker']
