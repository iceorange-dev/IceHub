"""Safe storage primitives used by IceHub.

This module deliberately has no Qt or third-party dependencies.  It owns the
rules for locating writable application data, serialising JSON without torn
writes, coordinating access between processes, and storing small secrets.
"""

from __future__ import annotations

import abc
import base64
import copy
import errno
import json
import os
import sys
import tempfile
import threading
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar


JSONValue = Any
T = TypeVar('T')
_MISSING = object()


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved read-only and writable locations for one IceHub process.

    Normal installations keep writable state below ``%LOCALAPPDATA%/IceHub``.
    Portable mode is intentionally opt-in: it is enabled only when a
    ``portable.flag`` file exists beside the executable.  Tests and source
    development can inject ``development_root`` and avoid touching the real
    user profile.
    """

    resource_dir: Path
    data_dir: Path
    cache_dir: Path
    log_dir: Path
    config_dir: Path
    temp_dir: Path
    tools_dir: Path
    portable: bool = False

    @classmethod
    def discover(
        cls,
        *,
        development_root: os.PathLike[str] | str | None = None,
        executable: os.PathLike[str] | str | None = None,
        local_appdata: os.PathLike[str] | str | None = None,
        resource_root: os.PathLike[str] | str | None = None,
    ) -> 'AppPaths':
        """Resolve paths without creating anything on disk.

        ``development_root`` is both the resource and writable data root.  It
        takes precedence over all runtime detection so unit tests are fully
        isolated.  ``executable`` and ``local_appdata`` are injectable mainly
        for deterministic tests and packaging smoke checks.
        """

        if development_root is not None:
            root = Path(development_root).expanduser().resolve()
            resources = Path(resource_root).expanduser().resolve() if resource_root is not None else root
            return cls._from_roots(resources, root, portable=False)

        packaged, packaged_dir = _packaged_executable_dir()
        explicit_executable = executable is not None
        if explicit_executable:
            executable_path = Path(executable).expanduser().resolve()
            executable_dir = executable_path.parent
            portable_allowed = True
        else:
            executable_dir = packaged_dir
            portable_allowed = packaged

        if resource_root is not None:
            resources = Path(resource_root).expanduser().resolve()
        else:
            resources = _resource_root(executable_dir, packaged or explicit_executable)

        portable = portable_allowed and (executable_dir / 'portable.flag').is_file()
        if portable:
            # Keeping all mutable files in a named child prevents them from
            # being mixed with binaries and packaged assets.
            data = executable_dir / 'data'
        else:
            local = Path(local_appdata).expanduser() if local_appdata is not None else _default_local_appdata()
            data = local.resolve() / 'IceHub'

        return cls._from_roots(resources, data, portable=portable)

    @classmethod
    def _from_roots(cls, resources: Path, data: Path, *, portable: bool) -> 'AppPaths':
        resources = resources.resolve()
        data = data.resolve()
        return cls(
            resource_dir=resources,
            data_dir=data,
            cache_dir=data / 'cache',
            log_dir=data / 'logs',
            config_dir=data / 'config',
            temp_dir=data / 'temp',
            tools_dir=data / 'tools',
            portable=portable,
        )

    # Short aliases make call sites read naturally while the ``*_dir`` names
    # remain explicit for integrations and documentation.
    @property
    def resource(self) -> Path:
        return self.resource_dir

    @property
    def data(self) -> Path:
        return self.data_dir

    @property
    def cache(self) -> Path:
        return self.cache_dir

    @property
    def log(self) -> Path:
        return self.log_dir

    def ensure_writable_dirs(self) -> 'AppPaths':
        """Create all writable directories and return ``self``."""

        for path in (
            self.data_dir,
            self.cache_dir,
            self.log_dir,
            self.config_dir,
            self.temp_dir,
            self.tools_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def resource_path(self, relative: os.PathLike[str] | str) -> Path:
        """Return a resource path, rejecting absolute paths and traversal."""

        relative_path = Path(relative)
        if relative_path.is_absolute():
            raise ValueError('resource path must be relative')
        candidate = (self.resource_dir / relative_path).resolve()
        try:
            candidate.relative_to(self.resource_dir.resolve())
        except ValueError as exc:
            raise ValueError('resource path escapes the resource directory') from exc
        return candidate


def _packaged_executable_dir() -> tuple[bool, Path]:
    compiled = globals().get('__compiled__')
    containing_dir = getattr(compiled, 'containing_dir', None)
    if containing_dir:
        return True, Path(containing_dir).resolve()
    if getattr(sys, 'frozen', False):
        return True, Path(sys.executable).resolve().parent
    # In source mode this value is used only when an explicit executable was
    # not supplied; portable mode remains disabled.
    return False, Path(sys.executable).resolve().parent


def _resource_root(executable_dir: Path, packaged: bool) -> Path:
    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        return Path(meipass).resolve()
    if packaged:
        return executable_dir.resolve()
    return Path(__file__).resolve().parent.parent


def _default_local_appdata() -> Path:
    configured = os.environ.get('LOCALAPPDATA')
    if configured:
        return Path(configured).expanduser()
    if os.name == 'nt':
        # LOCALAPPDATA is normally always defined by Explorer.  This fallback
        # also makes service/test launches deterministic without writing beside
        # the executable.
        return Path.home() / 'AppData' / 'Local'
    configured = os.environ.get('XDG_DATA_HOME')
    return Path(configured).expanduser() if configured else Path.home() / '.local' / 'share'


class FileLockTimeout(TimeoutError):
    """Raised when an inter-process lock cannot be acquired in time."""


_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}


def _process_lock_for(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.RLock())


class FileLock:
    """A small exclusive file lock implemented with the Python standard library.

    Windows uses ``msvcrt.locking``; POSIX uses ``fcntl.flock``.  A final
    atomic-directory fallback is retained for less common Python platforms.
    A process-local lock is also held because OS file-lock semantics for two
    threads in one process differ between platforms.
    """

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        timeout: float = 10.0,
        poll_interval: float = 0.05,
    ) -> None:
        if timeout < 0:
            raise ValueError('timeout must be non-negative')
        if poll_interval <= 0:
            raise ValueError('poll_interval must be positive')
        self.path = Path(path)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)
        self._process_lock = _process_lock_for(self.path)
        self._file: Any | None = None
        self._backend: str | None = None
        self._fallback_dir = self.path.with_name(self.path.name + '.d')
        self._process_lock_held = False

    def acquire(self) -> 'FileLock':
        if self._file is not None or self._backend == 'mkdir':
            return self

        deadline = time.monotonic() + self.timeout
        if not self._process_lock.acquire(timeout=self.timeout):
            raise FileLockTimeout(f'timed out waiting for lock: {self.path}')
        self._process_lock_held = True

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            file_obj = open(self.path, 'a+b')
            if file_obj.seek(0, os.SEEK_END) == 0:
                file_obj.write(b'\0')
                file_obj.flush()
            self._file = file_obj

            while True:
                try:
                    self._try_os_lock()
                    return self
                except (BlockingIOError, OSError) as exc:
                    if not _is_lock_contention(exc):
                        raise
                    if time.monotonic() >= deadline:
                        raise FileLockTimeout(f'timed out waiting for lock: {self.path}') from exc
                    time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        except BaseException:
            if self._file is not None:
                self._file.close()
                self._file = None
            if self._backend == 'mkdir':
                try:
                    self._fallback_dir.rmdir()
                except OSError:
                    pass
                self._backend = None
            if self._process_lock_held:
                self._process_lock.release()
                self._process_lock_held = False
            raise

    def _try_os_lock(self) -> None:
        assert self._file is not None
        if os.name == 'nt':
            try:
                import msvcrt
            except ImportError:
                self._try_fallback_lock()
                return
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            self._backend = 'msvcrt'
            return

        try:
            import fcntl
        except ImportError:
            self._try_fallback_lock()
            return
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self._backend = 'fcntl'

    def _try_fallback_lock(self) -> None:
        try:
            self._fallback_dir.mkdir()
        except FileExistsError as exc:
            raise BlockingIOError(errno.EACCES, 'lock is held') from exc
        self._backend = 'mkdir'

    def release(self) -> None:
        if self._file is None and self._backend != 'mkdir':
            return
        try:
            if self._file is not None and self._backend == 'msvcrt':
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            elif self._file is not None and self._backend == 'fcntl':
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            elif self._backend == 'mkdir':
                self._fallback_dir.rmdir()
        finally:
            if self._file is not None:
                self._file.close()
                self._file = None
            self._backend = None
            if self._process_lock_held:
                self._process_lock.release()
                self._process_lock_held = False

    def __enter__(self) -> 'FileLock':
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


def _is_lock_contention(exc: OSError) -> bool:
    return exc.errno in {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLK,
        errno.EPERM,
        13,  # Windows CRT commonly reports only EACCES.
    }


class CorruptJsonError(ValueError):
    """A JSON store exists but cannot be decoded safely."""

    def __init__(self, path: Path, reason: BaseException) -> None:
        super().__init__(f'invalid JSON in {path}: {reason}')
        self.path = path
        self.reason = reason


class JsonStoreTypeError(TypeError):
    """A mapping operation was requested for a non-object JSON document."""


class AtomicJsonStore:
    """Thread/process-safe JSON storage with atomic replacement writes."""

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        default: JSONValue = None,
        lock_timeout: float = 10.0,
    ) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + '.lock')
        self.default = {} if default is None else copy.deepcopy(default)
        self.lock_timeout = lock_timeout
        self._thread_lock = threading.RLock()

    def read(self, *, default: JSONValue = _MISSING) -> JSONValue:
        """Read a snapshot.

        Missing files return a deep copy of the configured default.  Malformed
        files raise :class:`CorruptJsonError` and are never replaced with
        defaults as a side effect.
        """

        with self._locked():
            return self._read_unlocked(default=default)

    def write(self, value: JSONValue) -> None:
        with self._locked():
            self._write_unlocked(value)

    def write_if_missing(self, value: JSONValue) -> bool:
        """Atomically create the document only when no document exists.

        The existence check and replacement write share the same process and
        inter-process lock, so concurrent first-run migrations cannot overwrite
        a configuration another IceHub process has just created.
        """

        with self._locked():
            if self.path.exists():
                return False
            self._write_unlocked(value)
            return True

    def update(self, transform: Callable[[JSONValue], JSONValue]) -> JSONValue:
        """Re-read, transform and atomically write a document under one lock."""

        with self._locked():
            current = self._read_unlocked()
            updated = transform(copy.deepcopy(current))
            self._write_unlocked(updated)
            return copy.deepcopy(updated)

    def update_fields(
        self,
        fields: Mapping[str, JSONValue] | None = None,
        /,
        **keyword_fields: JSONValue,
    ) -> dict[str, JSONValue]:
        """Patch top-level fields using the latest on-disk snapshot.

        This is intentionally not implemented as ``read(); write()``: the
        inter-process lock remains held for the complete read/modify/replace
        transaction, preventing lost updates from workers with stale config
        snapshots.
        """

        patch: dict[str, JSONValue] = dict(fields or {})
        patch.update(keyword_fields)

        def apply(current: JSONValue) -> dict[str, JSONValue]:
            if not isinstance(current, dict):
                raise JsonStoreTypeError(f'cannot update fields in non-object JSON store: {self.path}')
            result = dict(current)
            result.update(patch)
            return result

        updated = self.update(apply)
        assert isinstance(updated, dict)
        return updated

    def delete_fields(self, *names: str) -> dict[str, JSONValue]:
        def apply(current: JSONValue) -> dict[str, JSONValue]:
            if not isinstance(current, dict):
                raise JsonStoreTypeError(f'cannot delete fields in non-object JSON store: {self.path}')
            result = dict(current)
            for name in names:
                result.pop(name, None)
            return result

        updated = self.update(apply)
        assert isinstance(updated, dict)
        return updated

    def _locked(self) -> '_StoreLock':
        return _StoreLock(self._thread_lock, self.lock_path, self.lock_timeout)

    def _read_unlocked(self, *, default: JSONValue = _MISSING) -> JSONValue:
        if not self.path.exists():
            selected = self.default if default is _MISSING else default
            return copy.deepcopy(selected)
        try:
            with self.path.open('r', encoding='utf-8') as stream:
                return json.load(stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CorruptJsonError(self.path, exc) from exc

    def _write_unlocked(self, value: JSONValue) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f'.{self.path.name}.',
            suffix='.tmp',
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            _fsync_directory(self.path.parent)
        except BaseException:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise


class _StoreLock:
    def __init__(self, thread_lock: threading.RLock, path: Path, timeout: float) -> None:
        self._thread_lock = thread_lock
        self._file_lock = FileLock(path, timeout=timeout)

    def __enter__(self) -> None:
        acquired = self._thread_lock.acquire(timeout=self._file_lock.timeout)
        if not acquired:
            raise FileLockTimeout(f'timed out waiting for thread lock: {self._file_lock.path}')
        try:
            self._file_lock.acquire()
        except BaseException:
            self._thread_lock.release()
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self._file_lock.release()
        finally:
            self._thread_lock.release()


def _fsync_directory(directory: Path) -> None:
    """Persist the directory entry where supported (best effort on Windows)."""

    if os.name == 'nt':
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        # Atomic replacement has already succeeded.  Some filesystems do not
        # allow directory fsync, so lack of support is not a write failure.
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


class CredentialStoreError(RuntimeError):
    """Base error raised by credential storage implementations."""


class CredentialStoreUnavailable(CredentialStoreError):
    """Secure credential storage is unavailable on this runtime."""


class CredentialStore(abc.ABC):
    """Minimal credential-store abstraction used for the Ryuu auth key."""

    RYUU_AUTH_KEY = 'Ryuu_Auth_Key'
    backend: str
    secure: bool

    @abc.abstractmethod
    def get(self, name: str) -> str | None:
        raise NotImplementedError

    @abc.abstractmethod
    def set(self, name: str, value: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def delete(self, name: str) -> None:
        raise NotImplementedError

    def get_ryuu_auth_key(self) -> str | None:
        return self.get(self.RYUU_AUTH_KEY)

    def set_ryuu_auth_key(self, value: str) -> None:
        self.set(self.RYUU_AUTH_KEY, value)

    def delete_ryuu_auth_key(self) -> None:
        self.delete(self.RYUU_AUTH_KEY)


class _JsonCredentialStore(CredentialStore):
    _FORMAT = 'icehub-credentials-v1'

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self._store = AtomicJsonStore(path, default={})

    def get(self, name: str) -> str | None:
        _validate_credential_name(name)
        document = self._store.read()
        if not document:
            return None
        credentials = self._credentials_from(document)
        encoded = credentials.get(name)
        if encoded is None:
            return None
        if not isinstance(encoded, str):
            raise CredentialStoreError(f'credential {name!r} is not a string')
        return self._decode(encoded)

    def set(self, name: str, value: str) -> None:
        _validate_credential_name(name)
        if not isinstance(value, str):
            raise TypeError('credential value must be a string')
        encoded = self._encode(value)

        def apply(document: JSONValue) -> dict[str, JSONValue]:
            if not isinstance(document, dict):
                raise CredentialStoreError('credential file must contain a JSON object')
            result = dict(document)
            if result:
                self._validate_document(result)
            credentials = dict(result.get('credentials', {}))
            credentials[name] = encoded
            result.update(
                {
                    'format': self._FORMAT,
                    'protection': self.backend,
                    'credentials': credentials,
                }
            )
            return result

        self._store.update(apply)

    def delete(self, name: str) -> None:
        _validate_credential_name(name)

        def apply(document: JSONValue) -> dict[str, JSONValue]:
            if not isinstance(document, dict):
                raise CredentialStoreError('credential file must contain a JSON object')
            if not document:
                return {
                    'format': self._FORMAT,
                    'protection': self.backend,
                    'credentials': {},
                }
            self._validate_document(document)
            result = dict(document)
            credentials = dict(result.get('credentials', {}))
            credentials.pop(name, None)
            result['credentials'] = credentials
            return result

        self._store.update(apply)

    def _credentials_from(self, document: JSONValue) -> dict[str, JSONValue]:
        if not isinstance(document, dict):
            raise CredentialStoreError('credential file must contain a JSON object')
        self._validate_document(document)
        credentials = document.get('credentials', {})
        if not isinstance(credentials, dict):
            raise CredentialStoreError('credential file has an invalid credentials field')
        return credentials

    def _validate_document(self, document: dict[str, JSONValue]) -> None:
        if document.get('format') != self._FORMAT:
            raise CredentialStoreError('credential file has an unknown format')
        if document.get('protection') != self.backend:
            raise CredentialStoreError(f'credential file uses {document.get("protection")!r}, not {self.backend!r}')
        if not isinstance(document.get('credentials', {}), dict):
            raise CredentialStoreError('credential file has an invalid credentials field')

    @abc.abstractmethod
    def _encode(self, value: str) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def _decode(self, value: str) -> str:
        raise NotImplementedError


class DpapiCredentialStore(_JsonCredentialStore):
    """Per-user Windows DPAPI encrypted JSON credential storage."""

    backend = 'windows-dpapi'
    secure = True

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self._dpapi = _DpapiProvider()
        super().__init__(path)

    def _encode(self, value: str) -> str:
        protected = self._dpapi.protect(value.encode('utf-8'))
        return base64.b64encode(protected).decode('ascii')

    def _decode(self, value: str) -> str:
        try:
            protected = base64.b64decode(value, validate=True)
            return self._dpapi.unprotect(protected).decode('utf-8')
        except (ValueError, UnicodeDecodeError) as exc:
            raise CredentialStoreError('credential payload is invalid') from exc


class PlaintextCredentialStore(_JsonCredentialStore):
    """Explicit, visibly labelled fallback when secure storage is unavailable."""

    backend = 'plaintext'
    secure = False

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        acknowledge_insecure_storage: bool = False,
    ) -> None:
        if not acknowledge_insecure_storage:
            raise CredentialStoreUnavailable('plaintext credential storage requires explicit acknowledgement')
        warnings.warn(
            'IceHub credentials will be stored as plaintext; DPAPI is unavailable or disabled',
            RuntimeWarning,
            stacklevel=2,
        )
        super().__init__(path)

    def _encode(self, value: str) -> str:
        return value

    def _decode(self, value: str) -> str:
        return value


def create_credential_store(
    path: os.PathLike[str] | str,
    *,
    allow_plaintext_fallback: bool = False,
) -> CredentialStore:
    """Create the best credential store available on this platform.

    There is no silent downgrade.  Callers must opt in to plaintext fallback,
    which emits a runtime warning and records ``"protection": "plaintext"``
    in the file rather than pretending that the value is encrypted.
    """

    dpapi_error: BaseException | None = None
    if os.name == 'nt':
        try:
            return DpapiCredentialStore(path)
        except CredentialStoreUnavailable as exc:
            dpapi_error = exc
    else:
        dpapi_error = CredentialStoreUnavailable('Windows DPAPI is not available')

    if not allow_plaintext_fallback:
        raise CredentialStoreUnavailable(
            'secure credential storage is unavailable; explicitly enable plaintext fallback '
            'only if this risk is acceptable'
        ) from dpapi_error
    return PlaintextCredentialStore(path, acknowledge_insecure_storage=True)


def _validate_credential_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip() or '\x00' in name:
        raise ValueError('credential name must be a non-empty string without NUL bytes')


class _DpapiProvider:
    _DESCRIPTION = 'IceHub credential'
    _UI_FORBIDDEN = 0x1

    def __init__(self) -> None:
        if os.name != 'nt':
            raise CredentialStoreUnavailable('Windows DPAPI is not available')
        try:
            import ctypes
            from ctypes import wintypes

            class DataBlob(ctypes.Structure):
                _fields_ = [
                    ('cbData', wintypes.DWORD),
                    ('pbData', ctypes.POINTER(ctypes.c_ubyte)),
                ]

            self._ctypes = ctypes
            self._blob_type = DataBlob
            self._crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
            self._kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
            self._crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(DataBlob),
                wintypes.LPCWSTR,
                ctypes.POINTER(DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            self._crypt32.CryptProtectData.restype = wintypes.BOOL
            self._crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(DataBlob),
                ctypes.c_void_p,
                ctypes.POINTER(DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(DataBlob),
            ]
            self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
            self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            self._kernel32.LocalFree.restype = ctypes.c_void_p
        except (AttributeError, OSError) as exc:
            raise CredentialStoreUnavailable(f'failed to initialise Windows DPAPI: {exc}') from exc

    def _input_blob(self, data: bytes) -> tuple[Any, Any]:
        ctypes = self._ctypes
        buffer = (ctypes.c_ubyte * max(1, len(data)))()
        if data:
            ctypes.memmove(buffer, data, len(data))
        blob = self._blob_type(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        return blob, buffer

    def protect(self, data: bytes) -> bytes:
        ctypes = self._ctypes
        input_blob, input_buffer = self._input_blob(data)
        output_blob = self._blob_type()
        # Keep input_buffer alive through the native call.
        _ = input_buffer
        succeeded = self._crypt32.CryptProtectData(
            ctypes.byref(input_blob),
            self._DESCRIPTION,
            None,
            None,
            None,
            self._UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        if not succeeded:
            error = ctypes.get_last_error()
            raise CredentialStoreError(f'CryptProtectData failed with Windows error {error}')
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            self._kernel32.LocalFree(output_blob.pbData)

    def unprotect(self, data: bytes) -> bytes:
        ctypes = self._ctypes
        input_blob, input_buffer = self._input_blob(data)
        output_blob = self._blob_type()
        _ = input_buffer
        succeeded = self._crypt32.CryptUnprotectData(
            ctypes.byref(input_blob),
            None,
            None,
            None,
            None,
            self._UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
        if not succeeded:
            error = ctypes.get_last_error()
            raise CredentialStoreError(f'CryptUnprotectData failed with Windows error {error}')
        try:
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            self._kernel32.LocalFree(output_blob.pbData)


__all__ = [
    'AppPaths',
    'AtomicJsonStore',
    'CorruptJsonError',
    'CredentialStore',
    'CredentialStoreError',
    'CredentialStoreUnavailable',
    'DpapiCredentialStore',
    'FileLock',
    'FileLockTimeout',
    'JsonStoreTypeError',
    'PlaintextCredentialStore',
    'create_credential_store',
]
