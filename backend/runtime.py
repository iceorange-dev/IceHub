"""Runtime dependency container for backend services.

The original application discovered paths and constructed stores as module
globals in ``ost_backend``.  That made tests depend on monkeypatching those
globals and made extracted modules easy to point at real user data by mistake.
``BackendRuntime`` keeps the same defaults while making every mutable boundary
explicit and injectable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
import threading
from typing import Protocol

from .storage import (
    AppPaths,
    AtomicJsonStore,
    CredentialStore,
    create_credential_store,
)


class CredentialFactory(Protocol):
    def __call__(
        self,
        path: Path,
        *,
        allow_plaintext_fallback: bool = False,
    ) -> CredentialStore: ...


@dataclass(slots=True)
class BackendRuntime:
    """All environment-specific paths, stores, caches, and shared logging."""

    paths: AppPaths
    legacy_root: Path
    log: logging.Logger = field(default_factory=lambda: logging.getLogger('IceHub'))
    credential_factory: CredentialFactory = create_credential_store
    config_store: AtomicJsonStore = field(init=False)
    name_store: AtomicJsonStore = field(init=False)
    header_store: AtomicJsonStore = field(init=False)
    sudama_store: AtomicJsonStore = field(init=False)
    steamless_manifest_store: AtomicJsonStore = field(init=False)
    name_cache: dict[str, str] = field(default_factory=dict, init=False)
    header_cache: dict[str, str] = field(default_factory=dict, init=False)
    cache_lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _credentials: CredentialStore | None = field(default=None, init=False, repr=False)
    _credential_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.legacy_root = Path(self.legacy_root).resolve(strict=False)
        self.config_store = AtomicJsonStore(self.config_file, default={})
        self.name_store = AtomicJsonStore(self.name_cache_file, default={})
        self.header_store = AtomicJsonStore(self.header_cache_file, default={})
        self.sudama_store = AtomicJsonStore(self.sudama_cache_file, default={})
        self.steamless_manifest_store = AtomicJsonStore(
            self.steamless_manifest,
            default={},
        )

    @classmethod
    def discover(cls) -> BackendRuntime:
        return cls(
            paths=AppPaths.discover(),
            legacy_root=Path(__file__).resolve().parent.parent,
        )

    @classmethod
    def from_paths(
        cls,
        paths: AppPaths,
        *,
        legacy_root: Path | None = None,
        log: logging.Logger | None = None,
        credential_factory: CredentialFactory = create_credential_store,
    ) -> BackendRuntime:
        return cls(
            paths=paths,
            legacy_root=legacy_root or paths.resource_dir,
            log=log or logging.getLogger('IceHub'),
            credential_factory=credential_factory,
        )

    @property
    def resource_root(self) -> Path:
        return self.paths.resource_dir

    @property
    def app_root(self) -> Path:
        return self.paths.data_dir

    @property
    def state_dir(self) -> Path:
        return self.paths.data_dir / 'state'

    @property
    def config_dir(self) -> Path:
        return self.paths.config_dir

    @property
    def config_file(self) -> Path:
        return self.config_dir / 'config.json'

    @property
    def credential_file(self) -> Path:
        return self.config_dir / 'credentials.json'

    @property
    def name_cache_file(self) -> Path:
        return self.paths.cache_dir / 'name_cache.json'

    @property
    def header_cache_file(self) -> Path:
        return self.paths.cache_dir / 'header_cache.json'

    @property
    def sudama_cache_file(self) -> Path:
        return self.paths.cache_dir / 'sudama_cache.json'

    @property
    def temp_dir(self) -> Path:
        return self.paths.temp_dir

    @property
    def tools_dir(self) -> Path:
        return self.paths.tools_dir

    @property
    def steamless_dir(self) -> Path:
        return self.tools_dir / 'steamless'

    @property
    def steamless_cli(self) -> Path:
        return self.steamless_dir / 'Steamless.CLI.exe'

    @property
    def steamless_manifest(self) -> Path:
        return self.state_dir / 'manifests' / 'steamless.json'

    def ensure_writable_dirs(self) -> None:
        self.paths.ensure_writable_dirs()

    def credentials(self) -> CredentialStore:
        """Construct the credential backend lazily and at most once."""

        with self._credential_lock:
            if self._credentials is None:
                self._credentials = self.credential_factory(
                    self.credential_file,
                    allow_plaintext_fallback=False,
                )
            return self._credentials


DEFAULT_RUNTIME = BackendRuntime.discover()


def resolve_runtime(runtime: BackendRuntime | None) -> BackendRuntime:
    return runtime or DEFAULT_RUNTIME


__all__ = [
    'BackendRuntime',
    'CredentialFactory',
    'DEFAULT_RUNTIME',
    'resolve_runtime',
]
