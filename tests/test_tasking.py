"""Lifecycle tests for Qt task coordination.

The complete module is skipped when PyQt6 is not installed.  Tests use a small
QCoreApplication pump instead of requiring pytest-qt, so they also run in the
minimal CI test environment.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable

import pytest


qt_core = pytest.importorskip(
    'PyQt6.QtCore',
    reason='a working PyQt6 runtime is required for Qt lifecycle tests',
    exc_type=ImportError,
)
QCoreApplication = qt_core.QCoreApplication
QEventLoop = qt_core.QEventLoop
QThread = qt_core.QThread

from app.tasking import (  # noqa: E402
    AsyncWorker,
    OperationCoordinator,
    ResourceBusyError,
    WorkerFailure,
    WorkerRegistry,
)


@pytest.fixture(scope='module')
def qt_app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def wait_until(
    app: QCoreApplication,
    predicate: Callable[[], bool],
    *,
    timeout: float = 2.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if predicate():
            app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
            return True
        time.sleep(0.005)
    app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
    return predicate()


def test_async_worker_emits_progress_result_and_finishes(qt_app: QCoreApplication) -> None:
    results: list[object] = []
    progress_values: list[object] = []
    failures: list[WorkerFailure] = []

    async def operation(progress):
        progress({'step': 1})
        await asyncio.sleep(0.01)
        return 42

    worker = AsyncWorker(operation)
    worker.progress.connect(progress_values.append)
    worker.result.connect(results.append)
    worker.error.connect(failures.append)
    worker.start()

    try:
        assert wait_until(qt_app, worker.isFinished)
        assert results == [42]
        assert progress_values == [{'step': 1}]
        assert failures == []
    finally:
        worker.request_cancel()
        worker.wait(1000)


def test_async_worker_preserves_exception_traceback(qt_app: QCoreApplication) -> None:
    failures: list[WorkerFailure] = []

    async def operation() -> None:
        raise ValueError('traceback sentinel')

    worker = AsyncWorker(operation)
    worker.error.connect(failures.append)
    worker.start()

    try:
        assert wait_until(qt_app, worker.isFinished)
        assert len(failures) == 1
        failure = failures[0]
        assert isinstance(failure.exception, ValueError)
        assert failure.message == 'traceback sentinel'
        assert 'ValueError: traceback sentinel' in failure.traceback
        assert 'operation' in failure.traceback
    finally:
        worker.wait(1000)


def test_request_cancel_runs_coroutine_finally(qt_app: QCoreApplication) -> None:
    started = threading.Event()
    cleaned_up = threading.Event()
    cancellations: list[bool] = []
    results: list[object] = []
    failures: list[object] = []

    async def operation() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleaned_up.set()

    worker = AsyncWorker(operation)
    worker.cancelled.connect(lambda: cancellations.append(True))
    worker.result.connect(results.append)
    worker.error.connect(failures.append)
    worker.start()

    try:
        assert started.wait(1)
        assert worker.request_cancel() is True
        assert worker.request_cancel() is False
        assert wait_until(qt_app, worker.isFinished)
        assert cleaned_up.is_set()
        assert cancellations == [True]
        assert results == []
        assert failures == []
    finally:
        worker.request_cancel()
        worker.wait(1000)


def test_registry_retains_worker_until_thread_finished(qt_app: QCoreApplication) -> None:
    registry = WorkerRegistry()
    cleanup_started = threading.Event()
    cleanup_done = threading.Event()
    retained_at_result: list[bool] = []

    async def lingering_task() -> None:
        try:
            await asyncio.sleep(60)
        finally:
            cleanup_started.set()
            await asyncio.sleep(0.05)
            cleanup_done.set()

    async def operation() -> str:
        asyncio.create_task(lingering_task())
        await asyncio.sleep(0)
        return 'ready'

    worker = AsyncWorker(operation)
    registry.register(worker)
    worker.result.connect(lambda _value: retained_at_result.append(worker in registry.workers()))
    worker.start()

    try:
        assert wait_until(qt_app, cleanup_started.is_set)
        assert retained_at_result == [True]
        assert worker in registry.workers()
        assert wait_until(qt_app, worker.isFinished)
        assert cleanup_done.is_set()
        assert wait_until(qt_app, lambda: worker not in registry.workers())
    finally:
        registry.cancel_all()
        registry.wait_for_all(1000)


def test_registry_start_failure_restores_previous_ownership_state(
    qt_app: QCoreApplication,
) -> None:
    del qt_app

    class FailingStartThread(QThread):
        def start(
            self,
            priority: QThread.Priority = QThread.Priority.InheritPriority,
        ) -> None:
            del priority
            raise RuntimeError('cannot start thread')

    registry = WorkerRegistry()
    added: list[object] = []
    removed: list[object] = []
    registry.workerAdded.connect(added.append)
    registry.workerRemoved.connect(removed.append)
    new_worker = FailingStartThread()

    with pytest.raises(RuntimeError, match='cannot start thread'):
        registry.start(new_worker)

    assert registry.workers() == ()
    assert added == [new_worker]
    assert removed == [new_worker]

    existing_worker = FailingStartThread()
    registry.register(existing_worker)
    with pytest.raises(RuntimeError, match='cannot start thread'):
        registry.start(existing_worker)
    assert registry.workers() == (existing_worker,)
    assert registry.unregister(existing_worker) is True


def test_registry_cancel_and_wait_are_cooperative(qt_app: QCoreApplication) -> None:
    registry = WorkerRegistry()
    started = threading.Event()
    cleaned_up = threading.Event()

    async def operation() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleaned_up.set()

    worker = AsyncWorker(operation)
    registry.start(worker)

    try:
        assert started.wait(1)
        assert registry.cancel_all() == 1
        assert registry.wait_for_all(1000) is True
        assert cleaned_up.is_set()
        assert registry.workers() == ()
    finally:
        registry.cancel_all()
        registry.wait_for_all(1000)
        qt_app.processEvents()


def test_operation_coordinator_is_atomic_and_tokens_are_single_use() -> None:
    coordinator = OperationCoordinator()
    changes: list[tuple[str, bool]] = []
    global_changes: list[bool] = []
    coordinator.busyChanged.connect(lambda name, busy: changes.append((name, busy)))
    coordinator.anyBusyChanged.connect(global_changes.append)

    token = coordinator.acquire(('steam', 'lua'), owner='install')
    assert token is not None
    assert token.resources == ('steam', 'lua')
    assert coordinator.is_busy()
    assert coordinator.active_resources() == ('steam', 'lua')

    # Multi-resource acquisition is all-or-nothing when one name conflicts.
    assert coordinator.acquire(('lua', 'steamless'), owner='other') is None
    assert not coordinator.is_busy('steamless')
    assert coordinator.conflicting_resources(('lua', 'steamless')) == ('lua',)

    assert token.release() is True
    assert token.released
    assert token.release() is False
    assert not coordinator.is_busy()
    assert changes == [
        ('steam', True),
        ('lua', True),
        ('steam', False),
        ('lua', False),
    ]
    assert global_changes == [True, False]


def test_operation_context_releases_on_error_and_rejects_conflict() -> None:
    coordinator = OperationCoordinator()

    with pytest.raises(RuntimeError, match='operation failed'):
        with coordinator.operation('steam') as token:
            assert coordinator.holder('steam') is token
            raise RuntimeError('operation failed')
    assert not coordinator.is_busy('steam')

    token = coordinator.acquire('steam')
    assert token is not None
    try:
        with pytest.raises(ResourceBusyError) as failure:
            with coordinator.operation(('steam', 'lua')):
                pytest.fail('busy operation context should not be entered')
        assert failure.value.resources == ('steam',)
        assert not coordinator.is_busy('lua')
    finally:
        token.release()
