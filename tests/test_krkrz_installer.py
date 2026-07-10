from __future__ import annotations

from pathlib import Path

import backend.ost_backend as facade
import pytest
from backend.constants import (
    KRKRZ_BUNDLED_PAYLOAD_REL,
    KRKRZ_BUNDLED_REL,
    KRKRZ_DLL_FILENAME,
)
from backend.installers import krkrz
from backend.runtime import BackendRuntime
from backend.storage import AppPaths


def _pe(payload: bytes) -> bytes:
    return b'MZ' + (b'\0' * 62) + payload


def _runtime(tmp_path: Path) -> BackendRuntime:
    paths = AppPaths.discover(
        development_root=tmp_path / 'data',
        resource_root=tmp_path / 'resources',
    )
    paths.ensure_writable_dirs()
    return BackendRuntime.from_paths(paths)


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_legacy_facade_statically_reexports_krkrz_domain() -> None:
    exports = (
        'bundled_krkrz_dll',
        'deploy_krkrz_patch',
        'detect_krkrz_status',
        'remove_krkrz_patch',
        '_select_legacy_krkrz_original',
    )
    for name in exports:
        assert getattr(facade, name) is getattr(krkrz, name)

    assert facade._KRKRZ_BACKUP_RE is krkrz._KRKRZ_BACKUP_RE
    assert facade.KRKRZ_REPO == krkrz.KRKRZ_REPO


def test_bundled_patch_prefers_dll_then_uses_staged_payload_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dll = tmp_path / 'assets' / 'krkrz' / 'Version.dll'
    payload = tmp_path / 'assets' / 'krkrz' / 'Version.bin'
    candidates = {
        KRKRZ_BUNDLED_REL: dll,
        KRKRZ_BUNDLED_PAYLOAD_REL: payload,
    }
    monkeypatch.setattr(
        krkrz,
        'resource_path',
        lambda relative, *, runtime=None: candidates[relative],
    )

    _write(payload, _pe(b'staged payload'))
    assert krkrz.bundled_krkrz_dll() == payload

    _write(dll, _pe(b'development DLL'))
    assert krkrz.bundled_krkrz_dll() == dll


def test_direct_domain_deploy_status_and_remove_restores_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    patch_bytes = _pe(b'IceHub KRKRZ patch')
    original_bytes = _pe(b'game original Version.dll')
    bundled = _write(runtime.resource_root / KRKRZ_BUNDLED_REL, patch_bytes)
    game = tmp_path / 'game'
    target = _write(game / KRKRZ_DLL_FILENAME, original_bytes)
    _write(game / 'data.xp3', b'archive marker')
    monkeypatch.setattr(
        krkrz,
        'bundled_krkrz_dll',
        lambda *, runtime=None: bundled,
    )

    assert krkrz.bundled_krkrz_dll(runtime=runtime) == bundled

    success, message, deployed = krkrz.deploy_krkrz_patch(game, runtime=runtime)

    assert success is True, message
    assert deployed == target
    assert target.read_bytes() == patch_bytes
    assert krkrz.detect_krkrz_status(game, runtime=runtime) == {
        'deployed': True,
        'has_backup': True,
        'looks_like_krkrz': True,
        'managed': True,
        'integrity_ok': True,
    }

    success, message = krkrz.remove_krkrz_patch(game, runtime=runtime)

    assert success is True, message
    assert target.read_bytes() == original_bytes
    status = krkrz.detect_krkrz_status(game, runtime=runtime)
    assert status['deployed'] is False
    assert status['managed'] is False
    assert status['looks_like_krkrz'] is True


def test_direct_domain_refuses_unmanaged_overwrite_without_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime(tmp_path)
    bundled = _write(runtime.resource_root / KRKRZ_BUNDLED_REL, _pe(b'IceHub patch'))
    monkeypatch.setattr(
        krkrz,
        'bundled_krkrz_dll',
        lambda *, runtime=None: bundled,
    )
    game = tmp_path / 'game'
    unmanaged = _write(game / KRKRZ_DLL_FILENAME, _pe(b'third-party loader'))

    success, _message, deployed = krkrz.deploy_krkrz_patch(
        game,
        backup_existing=False,
        runtime=runtime,
    )

    assert success is False
    assert deployed is None
    assert unmanaged.read_bytes() == _pe(b'third-party loader')
