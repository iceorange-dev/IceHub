from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import backend.storage as storage_module
from backend.storage import (
    AppPaths,
    AtomicJsonStore,
    CorruptJsonError,
    CredentialStoreUnavailable,
    DpapiCredentialStore,
    FileLock,
    FileLockTimeout,
    PlaintextCredentialStore,
    create_credential_store,
)


def test_development_root_is_fully_injectable(tmp_path: Path) -> None:
    root = tmp_path / 'checkout'

    paths = AppPaths.discover(development_root=root)

    assert paths.resource_dir == root.resolve()
    assert paths.data_dir == root.resolve()
    assert paths.config_dir == root.resolve() / 'config'
    assert paths.cache_dir == root.resolve() / 'cache'
    assert paths.log_dir == root.resolve() / 'logs'
    assert paths.temp_dir == root.resolve() / 'temp'
    assert paths.tools_dir == root.resolve() / 'tools'
    assert not paths.portable


def test_normal_install_uses_local_appdata(tmp_path: Path) -> None:
    install = tmp_path / 'install'
    local = tmp_path / 'profile' / 'AppData' / 'Local'

    paths = AppPaths.discover(
        executable=install / 'IceHub.exe',
        local_appdata=local,
        resource_root=install,
    )

    assert paths.resource_dir == install.resolve()
    assert paths.data_dir == local.resolve() / 'IceHub'
    assert paths.cache_dir == local.resolve() / 'IceHub' / 'cache'
    assert not paths.portable


def test_portable_flag_is_required_and_uses_named_data_directory(tmp_path: Path) -> None:
    install = tmp_path / 'portable'
    install.mkdir()
    (install / 'portable.flag').touch()

    paths = AppPaths.discover(
        executable=install / 'IceHub.exe',
        local_appdata=tmp_path / 'must-not-be-used',
    )

    assert paths.portable
    assert paths.resource_dir == install.resolve()
    assert paths.data_dir == install.resolve() / 'data'
    assert paths.config_dir == install.resolve() / 'data' / 'config'
    assert paths.tools_dir == install.resolve() / 'data' / 'tools'


def test_app_paths_create_only_writable_directories(tmp_path: Path) -> None:
    resources = tmp_path / 'missing-resources'
    data = tmp_path / 'state'
    paths = AppPaths.discover(development_root=data, resource_root=resources)

    returned = paths.ensure_writable_dirs()

    assert returned is paths
    assert not resources.exists()
    for directory in (
        paths.data_dir,
        paths.cache_dir,
        paths.log_dir,
        paths.config_dir,
        paths.temp_dir,
        paths.tools_dir,
    ):
        assert directory.is_dir()


def test_resource_path_rejects_escape(tmp_path: Path) -> None:
    paths = AppPaths.discover(development_root=tmp_path)

    assert paths.resource_path('assets/icon.ico') == tmp_path / 'assets' / 'icon.ico'
    with pytest.raises(ValueError, match='escapes'):
        paths.resource_path('../secret')


def test_atomic_write_replaces_from_same_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / 'config.json'
    store = AtomicJsonStore(path)
    store.write({'generation': 1})
    real_replace = storage_module.os.replace
    replacements: list[tuple[Path, Path]] = []

    def observed_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        source_path = Path(source)
        destination_path = Path(destination)
        # Until the single replace call, readers still see the complete old
        # generation and the complete new generation lives in a sibling temp.
        assert json.loads(path.read_text(encoding='utf-8')) == {'generation': 1}
        assert json.loads(source_path.read_text(encoding='utf-8')) == {'generation': 2}
        assert source_path.parent == destination_path.parent == tmp_path
        replacements.append((source_path, destination_path))
        real_replace(source_path, destination_path)

    monkeypatch.setattr(storage_module.os, 'replace', observed_replace)

    store.write({'generation': 2})

    assert len(replacements) == 1
    assert store.read() == {'generation': 2}
    assert not list(tmp_path.glob('.config.json.*.tmp'))


def test_failed_replace_preserves_previous_document_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / 'config.json'
    store = AtomicJsonStore(path)
    store.write({'safe': 'old'})

    def fail_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        raise OSError('injected replace failure')

    monkeypatch.setattr(storage_module.os, 'replace', fail_replace)

    with pytest.raises(OSError, match='injected'):
        store.write({'safe': 'new'})

    assert json.loads(path.read_text(encoding='utf-8')) == {'safe': 'old'}
    assert not list(tmp_path.glob('.config.json.*.tmp'))


def test_corrupt_json_is_reported_without_overwrite(tmp_path: Path) -> None:
    path = tmp_path / 'config.json'
    original = b'{"unfinished": true'
    path.write_bytes(original)
    store = AtomicJsonStore(path, default={'replacement': 'must not happen'})

    with pytest.raises(CorruptJsonError) as read_error:
        store.read()
    assert read_error.value.path == path
    assert path.read_bytes() == original

    with pytest.raises(CorruptJsonError):
        store.update_fields({'new': 'value'})
    assert path.read_bytes() == original


def test_missing_store_returns_independent_default_without_writing(tmp_path: Path) -> None:
    path = tmp_path / 'missing.json'
    store = AtomicJsonStore(path, default={'nested': []})

    first = store.read()
    first['nested'].append('local mutation')

    assert store.read() == {'nested': []}
    assert not path.exists()


def test_write_if_missing_preserves_existing_value(tmp_path: Path) -> None:
    store = AtomicJsonStore(tmp_path / 'config.json', default={})

    assert store.write_if_missing({'owner': 'first'}) is True
    assert store.write_if_missing({'owner': 'second'}) is False
    assert store.read() == {'owner': 'first'}


def test_update_fields_rereads_and_preserves_unrelated_fields(tmp_path: Path) -> None:
    path = tmp_path / 'config.json'
    first_worker = AtomicJsonStore(path)
    stale_worker = AtomicJsonStore(path)
    first_worker.write({'theme': 'dark', 'source': 'ryuu'})
    stale_snapshot = stale_worker.read()

    first_worker.update_fields({'installed_ost_version': '1.2.3'})
    updated = stale_worker.update_fields({'installed_steamless_version': '3.1.0'})

    assert stale_snapshot == {'theme': 'dark', 'source': 'ryuu'}
    assert updated == {
        'theme': 'dark',
        'source': 'ryuu',
        'installed_ost_version': '1.2.3',
        'installed_steamless_version': '3.1.0',
    }
    assert AtomicJsonStore(path).read() == updated


def test_parallel_updates_from_independent_stores_do_not_lose_fields(tmp_path: Path) -> None:
    path = tmp_path / 'parallel.json'
    AtomicJsonStore(path).write({'seed': True})
    worker_count = 24
    barrier = threading.Barrier(worker_count)

    def update(index: int) -> None:
        barrier.wait(timeout=5)
        AtomicJsonStore(path, lock_timeout=10).update_fields({f'worker_{index}': index})

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(update, index) for index in range(worker_count)]
        for future in futures:
            future.result(timeout=15)

    document = AtomicJsonStore(path).read()
    assert document['seed'] is True
    assert {document[f'worker_{index}'] for index in range(worker_count)} == set(range(worker_count))


def test_file_lock_timeout_is_clear(tmp_path: Path) -> None:
    lock_path = tmp_path / 'resource.lock'
    started = threading.Event()
    outcome: list[BaseException] = []

    def contender() -> None:
        started.set()
        try:
            with FileLock(lock_path, timeout=0.05, poll_interval=0.005):
                pass
        except BaseException as exc:  # captured for assertion in the main thread
            outcome.append(exc)

    with FileLock(lock_path, timeout=1):
        thread = threading.Thread(target=contender)
        thread.start()
        assert started.wait(timeout=1)
        thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], FileLockTimeout)


def test_plaintext_credentials_require_explicit_acknowledgement(tmp_path: Path) -> None:
    path = tmp_path / 'credentials.json'

    with pytest.raises(CredentialStoreUnavailable, match='explicit acknowledgement'):
        PlaintextCredentialStore(path)

    with pytest.warns(RuntimeWarning, match='plaintext'):
        credentials = PlaintextCredentialStore(path, acknowledge_insecure_storage=True)
    credentials.set_ryuu_auth_key('visible-secret')

    document = json.loads(path.read_text(encoding='utf-8'))
    assert document['protection'] == 'plaintext'
    assert document['credentials']['Ryuu_Auth_Key'] == 'visible-secret'
    assert credentials.get_ryuu_auth_key() == 'visible-secret'
    credentials.delete_ryuu_auth_key()
    assert credentials.get_ryuu_auth_key() is None


def test_platform_credential_factory_never_silently_downgrades(tmp_path: Path) -> None:
    path = tmp_path / 'credentials.json'
    if os.name == 'nt':
        credentials = create_credential_store(path)
        assert isinstance(credentials, DpapiCredentialStore)
        credentials.set_ryuu_auth_key('dpapi-secret')
        assert credentials.get_ryuu_auth_key() == 'dpapi-secret'
        assert b'dpapi-secret' not in path.read_bytes()
        assert json.loads(path.read_text(encoding='utf-8'))['protection'] == 'windows-dpapi'
    else:
        with pytest.raises(CredentialStoreUnavailable, match='explicitly enable'):
            create_credential_store(path)
        with pytest.warns(RuntimeWarning, match='plaintext'):
            credentials = create_credential_store(path, allow_plaintext_fallback=True)
        assert isinstance(credentials, PlaintextCredentialStore)
