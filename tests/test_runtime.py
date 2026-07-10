from __future__ import annotations

import logging
from pathlib import Path

from backend.runtime import BackendRuntime, resolve_runtime
from backend.storage import AppPaths, CredentialStore


class FakeCredentials(CredentialStore):
    backend = 'fake'
    secure = True

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def test_runtime_derives_every_mutable_path_from_app_paths(tmp_path: Path) -> None:
    paths = AppPaths.discover(
        development_root=tmp_path / 'data',
        resource_root=tmp_path / 'resources',
    )
    runtime = BackendRuntime.from_paths(paths, legacy_root=tmp_path / 'legacy')

    assert runtime.resource_root == (tmp_path / 'resources').resolve()
    assert runtime.app_root == (tmp_path / 'data').resolve()
    assert runtime.state_dir == runtime.app_root / 'state'
    assert runtime.config_file == runtime.app_root / 'config' / 'config.json'
    assert runtime.credential_file == runtime.app_root / 'config' / 'credentials.json'
    assert runtime.name_store.path == runtime.app_root / 'cache' / 'name_cache.json'
    assert runtime.header_store.path == runtime.app_root / 'cache' / 'header_cache.json'
    assert runtime.sudama_store.path == runtime.app_root / 'cache' / 'sudama_cache.json'
    assert runtime.steamless_dir == runtime.app_root / 'tools' / 'steamless'
    assert runtime.steamless_cli == runtime.steamless_dir / 'Steamless.CLI.exe'
    assert runtime.steamless_manifest_store.path == (
        runtime.state_dir / 'manifests' / 'steamless.json'
    )


def test_runtime_construction_has_no_filesystem_write_side_effect(tmp_path: Path) -> None:
    data_root = tmp_path / 'not-created'
    paths = AppPaths.discover(
        development_root=data_root,
        resource_root=tmp_path / 'resources',
    )

    BackendRuntime.from_paths(paths)

    assert not data_root.exists()


def test_credentials_are_lazy_and_cached(tmp_path: Path) -> None:
    paths = AppPaths.discover(development_root=tmp_path / 'data')
    calls: list[tuple[Path, bool]] = []
    credentials = FakeCredentials()

    def factory(path: Path, *, allow_plaintext_fallback: bool = False) -> CredentialStore:
        calls.append((path, allow_plaintext_fallback))
        return credentials

    runtime = BackendRuntime.from_paths(paths, credential_factory=factory)
    assert calls == []

    assert runtime.credentials() is credentials
    assert runtime.credentials() is credentials
    assert calls == [(runtime.credential_file, False)]


def test_runtime_owns_independent_caches_and_logger(tmp_path: Path) -> None:
    first_paths = AppPaths.discover(development_root=tmp_path / 'first')
    second_paths = AppPaths.discover(development_root=tmp_path / 'second')
    logger = logging.getLogger('IceHub.runtime-test')
    first = BackendRuntime.from_paths(first_paths, log=logger)
    second = BackendRuntime.from_paths(second_paths)

    first.name_cache['480'] = 'Spacewar'

    assert first.log is logger
    assert second.name_cache == {}
    assert resolve_runtime(first) is first
