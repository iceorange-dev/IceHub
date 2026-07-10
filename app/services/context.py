"""Typed application services shared by the desktop pages."""

from __future__ import annotations

from functools import partial
import threading
from typing import Protocol
import weakref

from PyQt6.QtCore import QObject
from PyQt6.QtWidgets import QWidget

from app.tasking import OperationCoordinator, WorkerRegistry
from backend.ost_backend import OSTBackend
from backend.runtime import BackendRuntime, resolve_runtime


class BackendProvider(Protocol):
    """Create a fresh backend whose async client belongs to one worker loop.

    A provider must return a different live instance on every call. Reusing an
    ``OSTBackend`` across UI workers can share one ``httpx.AsyncClient`` across
    event loops and is therefore unsupported.
    """

    def __call__(self) -> OSTBackend: ...


class _FreshBackendProvider:
    """Reject custom factories that return an already-live backend instance."""

    def __init__(self, provider: BackendProvider) -> None:
        self._provider = provider
        self._lock = threading.Lock()
        self._live: dict[int, weakref.ReferenceType[OSTBackend]] = {}

    def __call__(self) -> OSTBackend:
        backend = self._provider()
        identity = id(backend)
        owner_ref = weakref.ref(self)

        def discard(reference: weakref.ReferenceType[OSTBackend]) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._discard(identity, reference)

        try:
            reference = weakref.ref(backend, discard)
        except TypeError as error:
            raise TypeError(
                'backend providers must return weak-referenceable instances'
            ) from error

        with self._lock:
            previous = self._live.get(identity)
            if previous is not None and previous() is backend:
                raise RuntimeError(
                    'backend_provider reused a live backend instance; '
                    'return a fresh OSTBackend for every call'
                )
            self._live[identity] = reference
        return backend

    def _discard(
        self,
        identity: int,
        reference: weakref.ReferenceType[OSTBackend],
    ) -> None:
        with self._lock:
            if self._live.get(identity) is reference:
                self._live.pop(identity, None)


def _guard_backend_provider(provider: BackendProvider) -> BackendProvider:
    """Guard every provider, including classes with custom ``__new__``."""

    return _FreshBackendProvider(provider)


class AppServices:
    """Long-lived application dependencies owned by the main window.

    Every provider is wrapped in a thread-safe freshness guard so a singleton
    backend fails before it can cross worker event loops.
    """

    def __init__(
        self,
        owner: QObject | None = None,
        *,
        backend_provider: BackendProvider = OSTBackend,
        runtime: BackendRuntime | None = None,
        worker_registry: WorkerRegistry | None = None,
        operation_coordinator: OperationCoordinator | None = None,
    ) -> None:
        self.runtime = resolve_runtime(runtime)
        provider = backend_provider
        if runtime is not None and backend_provider is OSTBackend:
            provider = partial(OSTBackend, runtime=self.runtime)
        self.backend_provider = _guard_backend_provider(provider)
        self.worker_registry = (
            worker_registry
            if worker_registry is not None
            else WorkerRegistry(owner)
        )
        self.operation_coordinator = (
            operation_coordinator
            if operation_coordinator is not None
            else OperationCoordinator(owner)
        )


def services_for(owner: QWidget) -> AppServices | None:
    """Resolve services from a page or its top-level window.

    Pages can receive ``app_services`` directly during migration.  The window
    lookup preserves compatibility for third-party page construction while
    keeping the service boundary typed and centralized.
    """

    direct = getattr(owner, 'app_services', None)
    if isinstance(direct, AppServices):
        return direct
    window = owner.window()
    shared = getattr(window, 'app_services', None)
    return shared if isinstance(shared, AppServices) else None


__all__ = ['AppServices', 'BackendProvider', 'services_for']
