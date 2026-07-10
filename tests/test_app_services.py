from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QWidget

from app.services.context import AppServices, services_for
from app.services.tasks import start_registered_worker
from app.tasking import OperationCoordinator, WorkerRegistry
from backend.runtime import BackendRuntime
from backend.storage import AppPaths


class DormantThread(QThread):
    def __init__(self) -> None:
        super().__init__()
        self.starts = 0

    def start(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.starts += 1


class FakeBackend:
    pass


def test_services_are_resolved_from_window(qtbot: Any) -> None:
    window = QWidget()
    page = QWidget(window)
    qtbot.addWidget(window)
    services = AppServices(window)
    window.app_services = services

    assert services_for(page) is services
    assert services.worker_registry.parent() is window
    assert services.operation_coordinator.parent() is window


def test_explicit_empty_task_services_are_preserved() -> None:
    registry = WorkerRegistry()
    coordinator = OperationCoordinator()

    services = AppServices(
        worker_registry=registry,
        operation_coordinator=coordinator,
    )

    assert services.worker_registry is registry
    assert services.operation_coordinator is coordinator


def test_backend_provider_rejects_a_reused_live_instance() -> None:
    singleton = FakeBackend()
    services = AppServices(backend_provider=lambda: singleton)

    assert services.backend_provider() is singleton
    with pytest.raises(RuntimeError, match='reused a live backend instance'):
        services.backend_provider()


def test_fresh_backend_provider_is_safe_for_parallel_callers() -> None:
    caller_count = 8
    barrier = threading.Barrier(caller_count)

    def create_backend() -> FakeBackend:
        barrier.wait(timeout=2)
        return FakeBackend()

    services = AppServices(backend_provider=create_backend)
    with ThreadPoolExecutor(max_workers=caller_count) as executor:
        futures = [
            executor.submit(services.backend_provider)
            for _ in range(caller_count)
        ]
        backends = [future.result(timeout=2) for future in futures]

    assert len({id(backend) for backend in backends}) == caller_count


def test_backend_class_provider_still_returns_fresh_instances() -> None:
    services = AppServices(backend_provider=FakeBackend)

    assert services.backend_provider() is not services.backend_provider()


def test_singleton_backend_class_is_rejected() -> None:
    class SingletonBackend:
        _instance = None

        def __new__(cls):
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    services = AppServices(backend_provider=SingletonBackend)

    backend = services.backend_provider()
    assert backend is SingletonBackend()
    with pytest.raises(RuntimeError, match='reused a live backend instance'):
        services.backend_provider()


def test_custom_runtime_is_used_by_the_default_backend_factory(
    tmp_path: Path,
) -> None:
    runtime = BackendRuntime.from_paths(
        AppPaths.discover(development_root=tmp_path / 'data')
    )
    services = AppServices(runtime=runtime)

    backend = services.backend_provider()

    assert services.runtime is runtime
    assert backend.runtime is runtime


def test_start_worker_uses_registry_and_resource_lease(qtbot: Any) -> None:
    window = QWidget()
    page = QWidget(window)
    qtbot.addWidget(window)
    services = AppServices(window)
    window.app_services = services
    worker = DormantThread()

    assert start_registered_worker(page, worker, 'lua') is True

    assert worker.starts == 1
    assert worker in services.worker_registry.workers()
    assert services.operation_coordinator.is_busy('lua')

    # The duplicate lease is rejected before a second worker is started.
    blocked = DormantThread()
    assert start_registered_worker(page, blocked, 'lua') is False
    assert blocked.starts == 0

    worker.finished.emit()
    assert not services.operation_coordinator.is_busy('lua')


def test_standalone_page_owns_worker_through_attached_registry(qtbot: Any) -> None:
    page = QWidget()
    qtbot.addWidget(page)
    started = threading.Event()
    release = threading.Event()

    class BlockingThread(QThread):
        def run(self) -> None:
            started.set()
            release.wait(timeout=2)

    worker = BlockingThread()
    try:
        assert start_registered_worker(page, worker) is True
        registry = page._icehub_worker_registry
        assert isinstance(registry, WorkerRegistry)
        assert registry.parent() is page
        assert worker in registry.workers()
        assert started.wait(1)

        release.set()
        assert worker.wait(1_000)
        qtbot.waitUntil(lambda: worker not in registry.workers(), timeout=1_000)
        assert registry.workers() == ()
    finally:
        release.set()
        worker.wait(1_000)
