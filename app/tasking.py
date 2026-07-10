"""Qt-friendly primitives for asynchronous and mutually exclusive operations.

The classes in this module deliberately use cooperative cancellation.  A worker
is never forcefully terminated: cancellation is delivered to its asyncio task,
and callers can then wait for the coroutine's ``finally`` blocks to finish.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
import traceback as traceback_module
import weakref
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeAlias, cast

from PyQt6.QtCore import QObject, QThread, pyqtSignal, pyqtSlot


ProgressCallback: TypeAlias = Callable[[object], None]
CoroutineFactory: TypeAlias = Callable[[], Awaitable[Any]] | Callable[[ProgressCallback], Awaitable[Any]]
CoroutineSource: TypeAlias = Awaitable[Any] | CoroutineFactory


@dataclass(frozen=True, slots=True)
class WorkerFailure:
    """An exception together with the traceback captured in the worker thread."""

    exception: BaseException
    traceback: str

    @classmethod
    def capture(cls, exception: BaseException) -> 'WorkerFailure':
        return cls(
            exception=exception,
            traceback=''.join(
                traceback_module.format_exception(
                    type(exception),
                    exception,
                    exception.__traceback__,
                )
            ),
        )

    @property
    def message(self) -> str:
        return str(self.exception)

    def __str__(self) -> str:
        return self.message or type(self.exception).__name__


class AsyncWorker(QThread):
    """Run one coroutine on a private asyncio event loop.

    ``source`` may be an already-created awaitable, a zero-argument factory, or
    a one-argument factory that receives :meth:`report_progress`.  Factories are
    useful when the coroutine needs a progress callback::

        worker = AsyncWorker(lambda progress: download(progress_cb=progress))

    The inherited :attr:`QThread.finished` signal is emitted only after event
    loop cleanup has completed.  ``error`` emits :class:`WorkerFailure`, which
    retains both the original exception and its formatted traceback.
    """

    result = pyqtSignal(object)
    error = pyqtSignal(object)
    progress = pyqtSignal(object)
    cancelled = pyqtSignal()

    def __init__(self, source: CoroutineSource, parent: QObject | None = None) -> None:
        super().__init__(parent)
        if not inspect.isawaitable(source) and not callable(source):
            raise TypeError('source must be an awaitable or an awaitable factory')
        self._source = source
        self._cancel_requested = threading.Event()
        self._state_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._has_run = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Future[Any] | None = None

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    def report_progress(self, value: object) -> None:
        """Emit a progress value from the coroutine's worker thread."""

        self.progress.emit(value)

    def request_cancel(self) -> bool:
        """Request cooperative cancellation and return whether it was new.

        The request is remembered if the thread has not started yet.  Once its
        event loop exists, ``Task.cancel`` is always scheduled with
        ``loop.call_soon_threadsafe``; this method never terminates the thread.
        """

        first_request = not self._cancel_requested.is_set()
        self._cancel_requested.set()
        self.requestInterruption()

        with self._state_lock:
            loop = self._loop
            task = self._task

        if loop is not None and task is not None and not task.done():
            try:
                loop.call_soon_threadsafe(task.cancel)
            except RuntimeError:
                # The event loop completed between the state check and call.
                pass
        return first_request

    def _create_awaitable(self) -> Awaitable[Any]:
        source = self._source
        if inspect.isawaitable(source):
            return cast(Awaitable[Any], source)

        factory = cast(CoroutineFactory, source)
        try:
            signature = inspect.signature(factory)
        except (TypeError, ValueError):
            # Callable objects without an inspectable signature follow the
            # documented one-argument factory form.
            awaitable = factory(self.report_progress)
        else:
            try:
                signature.bind(self.report_progress)
            except TypeError:
                try:
                    signature.bind()
                except TypeError as exception:
                    raise TypeError(
                        'coroutine factory must accept zero arguments or one progress callback'
                    ) from exception
                awaitable = factory()
            else:
                awaitable = factory(self.report_progress)

        if not inspect.isawaitable(awaitable):
            raise TypeError('coroutine factory must return an awaitable')
        return cast(Awaitable[Any], awaitable)

    @staticmethod
    def _shutdown_loop(loop: asyncio.AbstractEventLoop) -> None:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())

    def run(self) -> None:
        """QThread entry point; all loop state is finalized before returning."""

        loop: asyncio.AbstractEventLoop | None = None
        failure_reported = False

        with self._run_lock:
            if self._has_run:
                failure = RuntimeError('AsyncWorker instances are single-use')
                self.error.emit(WorkerFailure.capture(failure))
                return
            self._has_run = True

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            task = asyncio.ensure_future(self._create_awaitable(), loop=loop)
            with self._state_lock:
                self._loop = loop
                self._task = task

            if self._cancel_requested.is_set():
                task.cancel()

            result = loop.run_until_complete(task)
            self.result.emit(result)
        except asyncio.CancelledError:
            self.cancelled.emit()
        except BaseException as exception:
            failure_reported = True
            self.error.emit(WorkerFailure.capture(exception))
        finally:
            with self._state_lock:
                self._task = None
                self._loop = None

            if loop is not None:
                try:
                    self._shutdown_loop(loop)
                except BaseException as exception:
                    if not failure_reported:
                        self.error.emit(WorkerFailure.capture(exception))
                finally:
                    asyncio.set_event_loop(None)
                    loop.close()


class WorkerRegistry(QObject):
    """Own strong references to QThreads until their ``finished`` signal.

    Registering a worker before calling ``start`` prevents the common
    ``QThread: Destroyed while thread is still running`` failure.  Result and
    error signals never remove a worker; only actual thread completion does.
    """

    workerAdded = pyqtSignal(object)
    workerRemoved = pyqtSignal(object)
    changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._workers: set[QThread] = set()

    def register(self, worker: QThread) -> QThread:
        if not isinstance(worker, QThread):
            raise TypeError('worker must be a QThread')
        if worker.isFinished():
            raise ValueError('a finished worker cannot be registered')

        with self._lock:
            if worker in self._workers:
                return worker
            self._workers.add(worker)
            worker.finished.connect(self._on_worker_finished)

        self.workerAdded.emit(worker)
        self.changed.emit()

        # A running worker can finish between the precondition check and signal
        # connection.  The post-check closes that race without releasing a
        # worker that is still active.
        if worker.isFinished():
            self._remove_finished(worker)
        return worker

    def start(
        self,
        worker: QThread,
        priority: QThread.Priority = QThread.Priority.InheritPriority,
    ) -> QThread:
        with self._lock:
            was_registered = worker in self._workers
        self.register(worker)
        try:
            worker.start(priority)
        except BaseException:
            # Restore the exact pre-call ownership state.  QThread.start()
            # normally raises before the thread becomes active; unregister()
            # deliberately refuses to drop a genuinely running worker.
            if not was_registered:
                self.unregister(worker)
            raise
        return worker

    def workers(self) -> tuple[QThread, ...]:
        with self._lock:
            return tuple(self._workers)

    def running_workers(self) -> tuple[QThread, ...]:
        return tuple(worker for worker in self.workers() if worker.isRunning())

    def __len__(self) -> int:
        with self._lock:
            return len(self._workers)

    @pyqtSlot()
    def _on_worker_finished(self) -> None:
        worker = self.sender()
        if isinstance(worker, QThread):
            self._remove_finished(worker)

    def _remove_finished(self, worker: QThread) -> bool:
        if not worker.isFinished():
            return False
        with self._lock:
            if worker not in self._workers:
                return False
            self._workers.remove(worker)
            try:
                worker.finished.disconnect(self._on_worker_finished)
            except (TypeError, RuntimeError):
                pass
        self.workerRemoved.emit(worker)
        self.changed.emit()
        return True

    def unregister(self, worker: QThread) -> bool:
        """Release an idle worker; a running worker is never released."""

        if worker.isRunning():
            return False
        with self._lock:
            if worker not in self._workers:
                return False
            self._workers.remove(worker)
            try:
                worker.finished.disconnect(self._on_worker_finished)
            except (TypeError, RuntimeError):
                pass
        self.workerRemoved.emit(worker)
        self.changed.emit()
        return True

    def cancel(self, worker: QThread) -> bool:
        """Ask one registered worker to stop cooperatively."""

        with self._lock:
            if worker not in self._workers:
                return False

        request_cancel = getattr(worker, 'request_cancel', None)
        if callable(request_cancel):
            request_cancel()
            return True

        cancel = getattr(worker, 'cancel', None)
        if callable(cancel):
            cancel()
            return True

        worker.requestInterruption()
        return True

    def cancel_all(self) -> int:
        return sum(self.cancel(worker) for worker in self.workers())

    def wait_for_all(self, timeout_ms: int | None = None) -> bool:
        """Wait for registered workers without ever terminating them.

        ``None`` waits indefinitely.  A finite timeout is shared by all
        workers, rather than being applied once per worker.  The return value is
        true only when every registered worker has actually finished.
        """

        if timeout_ms is not None and timeout_ms < 0:
            raise ValueError('timeout_ms must be non-negative or None')
        deadline = None if timeout_ms is None else time.monotonic() + timeout_ms / 1000

        for worker in self.workers():
            if worker.isFinished():
                self._remove_finished(worker)
                continue
            if QThread.currentThread() is worker:
                continue

            if deadline is None:
                worker.wait()
            else:
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                if remaining_ms == 0 or not worker.wait(remaining_ms):
                    break
            if worker.isFinished():
                self._remove_finished(worker)

        for worker in self.workers():
            if worker.isFinished():
                self._remove_finished(worker)
        return len(self) == 0

    wait = wait_for_all


class ResourceBusyError(RuntimeError):
    """Raised when a context-managed operation cannot acquire its resources."""

    def __init__(self, resources: Iterable[str]) -> None:
        self.resources = tuple(resources)
        super().__init__(f"resources are busy: {', '.join(self.resources)}")


class OperationToken:
    """A single-use lease returned by :class:`OperationCoordinator`."""

    def __init__(
        self,
        coordinator: OperationCoordinator,
        resources: tuple[str, ...],
        owner: object | None,
    ) -> None:
        self._coordinator_ref = weakref.ref(coordinator)
        self._released = False
        self._lock = threading.Lock()
        self.resources = resources
        self.owner = owner

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    def _mark_released(self) -> bool:
        with self._lock:
            if self._released:
                return False
            self._released = True
            return True

    def release(self) -> bool:
        coordinator = self._coordinator_ref()
        if coordinator is None:
            self._mark_released()
            return False
        return coordinator.release(self)

    def __enter__(self) -> OperationToken:
        if self.released:
            raise RuntimeError('cannot re-enter a released operation token')
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.release()


class OperationCoordinator(QObject):
    """Atomically coordinate operations that share named resources."""

    busyChanged = pyqtSignal(str, bool)
    anyBusyChanged = pyqtSignal(bool)
    changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.RLock()
        self._resources: dict[str, OperationToken] = {}
        self._tokens: dict[int, OperationToken] = {}

    @staticmethod
    def _normalize_resources(resources: str | Iterable[str]) -> tuple[str, ...]:
        values = (resources,) if isinstance(resources, str) else tuple(resources)
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                raise TypeError('resource names must be strings')
            name = value.strip()
            if not name:
                raise ValueError('resource names cannot be empty')
            if name not in seen:
                normalized.append(name)
                seen.add(name)
        if not normalized:
            raise ValueError('at least one resource is required')
        return tuple(normalized)

    def acquire(
        self,
        resources: str | Iterable[str],
        *,
        owner: object | None = None,
    ) -> OperationToken | None:
        """Acquire every requested resource atomically, or return ``None``."""

        names = self._normalize_resources(resources)
        with self._lock:
            if any(name in self._resources for name in names):
                return None
            was_busy = bool(self._resources)
            token = OperationToken(self, names, owner)
            self._tokens[id(token)] = token
            for name in names:
                self._resources[name] = token

            for name in names:
                self.busyChanged.emit(name, True)
            if not was_busy:
                self.anyBusyChanged.emit(True)
            self.changed.emit()
            return token

    def release(self, token: OperationToken) -> bool:
        """Release a token once; foreign, stale, and duplicate releases fail."""

        if not isinstance(token, OperationToken):
            raise TypeError('token must be an OperationToken')
        with self._lock:
            if token._coordinator_ref() is not self:
                return False
            if self._tokens.get(id(token)) is not token:
                return False

            self._tokens.pop(id(token), None)
            released_names = tuple(
                name for name in token.resources if self._resources.get(name) is token
            )
            for name in released_names:
                self._resources.pop(name, None)
            token._mark_released()

            for name in released_names:
                self.busyChanged.emit(name, False)
            if not self._resources:
                self.anyBusyChanged.emit(False)
            self.changed.emit()
            return True

    def is_busy(self, resource: str | None = None) -> bool:
        with self._lock:
            if resource is None:
                return bool(self._resources)
            return resource.strip() in self._resources

    def active_resources(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._resources)

    def holder(self, resource: str) -> OperationToken | None:
        with self._lock:
            return self._resources.get(resource.strip())

    def conflicting_resources(self, resources: str | Iterable[str]) -> tuple[str, ...]:
        names = self._normalize_resources(resources)
        with self._lock:
            return tuple(name for name in names if name in self._resources)

    @contextmanager
    def operation(
        self,
        resources: str | Iterable[str],
        *,
        owner: object | None = None,
    ) -> Iterator[OperationToken]:
        """Acquire resources for a ``with`` block and release exactly once."""

        names = self._normalize_resources(resources)
        token = self.acquire(names, owner=owner)
        if token is None:
            raise ResourceBusyError(self.conflicting_resources(names))
        try:
            yield token
        finally:
            token.release()

    hold = operation


__all__ = [
    'AsyncWorker',
    'OperationCoordinator',
    'OperationToken',
    'ResourceBusyError',
    'WorkerFailure',
    'WorkerRegistry',
]
