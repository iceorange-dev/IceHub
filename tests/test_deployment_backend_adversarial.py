from __future__ import annotations

import importlib
import os
from pathlib import Path
import zipfile

import pytest

from backend.installers import krkrz as krkrz_installer
from backend.storage import AppPaths, AtomicJsonStore
from backend.runtime import BackendRuntime


OST_FILES = ('OpenSteamTool.dll', 'dwmapi.dll', 'xinput1_4.dll')


def _pe(payload: bytes) -> bytes:
    return b'MZ' + (b'\0' * 62) + payload


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _write_steamless_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('package/Steamless.CLI.exe', _pe(b'cli'))
        archive.writestr('package/Plugins/Steamless.Plugin.dll', _pe(b'plugin'))
    return path


@pytest.fixture
def isolated_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Keep destructive deployment tests away from real per-user state."""

    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local-app-data'))
    module = importlib.import_module('backend.ost_backend')
    paths = AppPaths.discover(
        development_root=tmp_path / 'icehub-data',
        resource_root=tmp_path / 'resources',
    )
    paths.ensure_writable_dirs()
    runtime = BackendRuntime.from_paths(paths, legacy_root=paths.resource_dir)
    return module, runtime


def test_legacy_ost_with_unknown_dlls_is_not_adopted_or_deleted(
    isolated_backend,
    tmp_path: Path,
) -> None:
    backend, runtime = isolated_backend
    steam = tmp_path / 'steam'
    unknown = {
        name: _pe(b'unmanaged-third-party-' + name.encode()) for name in OST_FILES
    }
    for name, content in unknown.items():
        _write(steam / name, content)

    success, _message, changed = backend.uninstall_ost(steam, runtime=runtime)

    assert success is False
    assert changed == []
    assert {name: (steam / name).read_bytes() for name in OST_FILES} == unknown
    assert not list((runtime.state_dir / 'manifests').glob('ost-*.json'))


def test_legacy_ost_uninstall_does_not_delete_unused_backup_content(
    isolated_backend,
    tmp_path: Path,
) -> None:
    backend, runtime = isolated_backend
    steam = tmp_path / 'steam'
    backup_dir = steam / '.ost_backup'
    originals = {name: _pe(b'original-' + name.encode()) for name in OST_FILES}
    for name in OST_FILES:
        _write(steam / name, _pe(b'legacy-ost-' + name.encode()))
        _write(backup_dir / name, originals[name])
    unrelated = _write(backup_dir / 'user-note.txt', b'not owned by IceHub')

    success, _message, changed = backend.uninstall_ost(steam, runtime=runtime)

    assert success is True
    assert set(OST_FILES).issubset(changed)
    assert {name: (steam / name).read_bytes() for name in OST_FILES} == originals
    assert unrelated.read_bytes() == b'not owned by IceHub'


def test_steamless_install_refuses_to_overwrite_directory_without_manifest(
    isolated_backend,
    tmp_path: Path,
) -> None:
    backend, runtime = isolated_backend
    existing = _write(
        runtime.steamless_dir / 'user-tool.txt',
        b'unmanaged tool directory',
    )
    archive = _write_steamless_zip(tmp_path / 'steamless.zip')

    success, _message, deployed = backend.install_steamless_from_zip(
        archive, runtime=runtime
    )

    assert success is False
    assert deployed == []
    assert existing.read_bytes() == b'unmanaged tool directory'
    assert not runtime.steamless_cli.exists()
    assert runtime.steamless_manifest_store.read() == {}


def test_steamless_package_rejects_nested_reserved_manifest_name(
    isolated_backend,
    tmp_path: Path,
) -> None:
    backend, runtime = isolated_backend
    archive = tmp_path / 'steamless-reserved.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr('package/Steamless.CLI.exe', _pe(b'cli'))
        bundle.writestr('package/Plugins/Steamless.Plugin.dll', _pe(b'plugin'))
        bundle.writestr('package/nested/.icehub-ownership.json', b'{}')

    success, _message, deployed = backend.install_steamless_from_zip(
        archive, runtime=runtime
    )

    assert success is False
    assert deployed == []
    assert not runtime.steamless_dir.exists()


def test_steamless_uninstall_refuses_directory_without_manifest(
    isolated_backend,
) -> None:
    backend, runtime = isolated_backend
    cli = _write(
        runtime.steamless_cli,
        _pe(b'unmanaged cli'),
    )
    extra = _write(
        runtime.steamless_dir / 'user-tool.txt',
        b'unmanaged content',
    )

    success, _message = backend.uninstall_steamless(runtime=runtime)

    assert success is False
    assert cli.read_bytes() == _pe(b'unmanaged cli')
    assert extra.read_bytes() == b'unmanaged content'
    assert runtime.steamless_dir.is_dir()


def test_stale_external_steamless_cache_does_not_authorize_deletion(
    isolated_backend,
) -> None:
    backend, runtime = isolated_backend
    cli = _write(
        runtime.steamless_cli,
        _pe(b'unmanaged cli'),
    )
    runtime.steamless_manifest_store.write({
        'schema': 1,
        'product': 'steamless',
        'version': 'stale',
        'files': {'Steamless.CLI.exe': backend.sha256_file(cli)},
    })

    success, _message = backend.uninstall_steamless(runtime=runtime)

    assert success is False
    assert cli.exists()


def test_interrupted_steamless_uninstall_restores_from_removal_journal(
    isolated_backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, runtime = isolated_backend
    class SimulatedProcessDeath(BaseException):
        pass

    archive = _write_steamless_zip(tmp_path / 'steamless.zip')
    installed, _message, _files = backend.install_steamless_from_zip(
        archive, runtime=runtime
    )
    assert installed is True
    journal_path = backend._steamless_remove_journal_path(runtime=runtime)
    real_write = AtomicJsonStore.write

    def die_before_commit(store, value):
        if store.path == journal_path and value.get('phase') == 'committed':
            raise SimulatedProcessDeath
        return real_write(store, value)

    with monkeypatch.context() as patch:
        patch.setattr(AtomicJsonStore, 'write', die_before_commit)
        with pytest.raises(SimulatedProcessDeath):
            backend.uninstall_steamless(runtime=runtime)

    assert not runtime.steamless_dir.exists()
    assert journal_path.exists()
    backend._recover_steamless_removal(runtime=runtime)

    assert runtime.steamless_cli.is_file()
    assert not journal_path.exists()
    assert not list(runtime.steamless_dir.parent.glob('.steamless-remove-*'))


def test_steamless_tampering_is_blocked_before_process_execution(
    isolated_backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, runtime = isolated_backend
    runner = importlib.import_module('backend.installers.steamless_runner')
    archive = _write_steamless_zip(tmp_path / 'steamless.zip')
    installed, _message, _files = backend.install_steamless_from_zip(
        archive, runtime=runtime
    )
    assert installed is True
    runtime.steamless_cli.write_bytes(_pe(b'tampered executable'))
    target = _write(tmp_path / 'game.exe', _pe(b'game'))

    def must_not_execute(*_args, **_kwargs):
        pytest.fail('tampered Steamless executable must not reach Popen')

    monkeypatch.setattr(runner.subprocess, 'Popen', must_not_execute)
    messages: list[str] = []
    success, code, output = backend.unpack_with_steamless(
        target, {}, output_cb=messages.append, runtime=runtime
    )

    assert success is False
    assert code == -1
    assert output is None
    assert any('完整性驗證失敗' in message for message in messages)


def test_executable_replace_failure_keeps_original_and_returns_verified_backup(
    isolated_backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, runtime = isolated_backend
    runner = importlib.import_module('backend.installers.steamless_runner')
    original_bytes = _pe(b'original game')
    unpacked_bytes = _pe(b'unpacked game')
    original = _write(tmp_path / 'game.exe', original_bytes)
    unpacked = _write(tmp_path / 'game.exe.unpacked.exe', unpacked_bytes)
    real_replace = runner.os.replace

    def fail_publish(source, destination):
        if Path(source) == unpacked and Path(destination) == original:
            raise OSError('simulated publish failure')
        return real_replace(source, destination)

    monkeypatch.setattr(runner.os, 'replace', fail_publish)
    success, message, backup = backend.replace_with_unpacked(
        original, unpacked, runtime=runtime
    )

    assert success is False
    assert 'simulated publish failure' in message
    assert backup is not None
    assert backup.read_bytes() == original_bytes
    assert original.read_bytes() == original_bytes
    assert unpacked.read_bytes() == unpacked_bytes


def test_executable_post_publish_permission_error_restores_original(
    isolated_backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, runtime = isolated_backend
    runner = importlib.import_module('backend.installers.steamless_runner')
    original_bytes = _pe(b'original game')
    unpacked_bytes = _pe(b'unpacked game')
    original = _write(tmp_path / 'game.exe', original_bytes)
    unpacked = _write(tmp_path / 'game.exe.unpacked.exe', unpacked_bytes)
    real_sha256 = runner.sha256_file
    injected = False

    def fail_first_new_target_hash(path):
        nonlocal injected
        candidate = Path(path)
        if candidate == original and candidate.read_bytes() == unpacked_bytes and not injected:
            injected = True
            raise PermissionError('simulated antivirus lock')
        return real_sha256(candidate)

    monkeypatch.setattr(runner, 'sha256_file', fail_first_new_target_hash)
    success, message, backup = backend.replace_with_unpacked(
        original, unpacked, runtime=runtime
    )

    assert success is False
    assert '已從備份還原原檔' in message
    assert backup is not None and backup.read_bytes() == original_bytes
    assert original.read_bytes() == original_bytes


def test_legacy_krkrz_restore_ignores_newer_backup_that_is_the_patch_itself(
    isolated_backend,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend, runtime = isolated_backend
    patch_bytes = _pe(b'IceHub patch')
    original_bytes = _pe(b'game original')
    bundled_patch = _write(tmp_path / 'resources' / 'Version.dll', patch_bytes)
    monkeypatch.setattr(
        krkrz_installer,
        'bundled_krkrz_dll',
        lambda *, runtime=None: bundled_patch,
    )

    game = tmp_path / 'game'
    current = _write(game / 'Version.dll', patch_bytes)
    original_backup = _write(game / 'version.dll.bak', original_bytes)
    duplicate_patch_backup = _write(game / 'version.dll.bak.2', patch_bytes)
    os.utime(original_backup, (1_000, 1_000))
    os.utime(duplicate_patch_backup, (2_000, 2_000))

    success, _message = backend.remove_krkrz_patch(game, runtime=runtime)

    assert success is True
    assert current.read_bytes() == original_bytes
    assert original_backup.read_bytes() == original_bytes
    assert duplicate_patch_backup.read_bytes() == patch_bytes
