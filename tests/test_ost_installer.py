from __future__ import annotations

from pathlib import Path

import backend.installers.ost as ost
import backend.ost_backend as facade
from backend.constants import OST_FILES
from backend.deployment import ManagedFileDeployment
from backend.runtime import BackendRuntime
from backend.storage import AppPaths


def _runtime(tmp_path: Path) -> BackendRuntime:
    paths = AppPaths.discover(development_root=tmp_path / 'data')
    paths.ensure_writable_dirs()
    return BackendRuntime.from_paths(paths)


def _pe(payload: bytes) -> bytes:
    return b'MZ' + (b'\0' * 62) + payload


def test_facade_statically_reexports_ost_domain() -> None:
    assert facade.detect_ost is ost.detect_ost
    assert facade.ost_installed is ost.ost_installed
    assert facade.install_ost_from_zip is ost.install_ost_from_zip
    assert facade.uninstall_ost is ost.uninstall_ost
    assert facade._prepare_legacy_ost_adoption is ost._prepare_legacy_ost_adoption
    assert facade._remove_all_managed_lua is ost._remove_all_managed_lua


def test_legacy_detection_requires_pe_shaped_files(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    steam = tmp_path / 'steam'
    steam.mkdir()
    (steam / 'OpenSteamTool.dll').write_bytes(_pe(b'ost'))
    (steam / 'dwmapi.dll').write_bytes(b'not-a-pe')
    (steam / 'xinput1_4.dll').write_bytes(_pe(b'loader'))

    status = ost.detect_ost(steam, runtime=runtime)

    assert status == {
        'OpenSteamTool.dll': True,
        'dwmapi.dll': False,
        'xinput1_4.dll': True,
    }
    assert ost.ost_installed(steam, runtime=runtime) is True


def test_managed_lua_cleanup_preserves_unowned_files(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    lua_dir = tmp_path / 'steam' / 'config' / 'lua'
    lua_dir.mkdir(parents=True)
    source = tmp_path / '480.lua'
    source.write_text('addappid(480)\n', encoding='utf-8')
    unmanaged = lua_dir / '999.lua'
    unmanaged.write_text('user-owned\n', encoding='utf-8')
    manager = ManagedFileDeployment(
        runtime.state_dir / 'library',
        'lua-480',
        lua_dir,
    )
    manager.install(
        {'480.lua': source},
        version='test',
        created_targets={'480.lua'},
    )

    removed, errors = ost._remove_all_managed_lua(
        lua_dir,
        runtime=runtime,
    )

    assert removed == 1
    assert errors == []
    assert not (lua_dir / '480.lua').exists()
    assert unmanaged.read_text(encoding='utf-8') == 'user-owned\n'
    assert set(OST_FILES) == {
        'OpenSteamTool.dll',
        'dwmapi.dll',
        'xinput1_4.dll',
    }
