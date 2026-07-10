"""Focused tests for the ownership-aware Lua library service."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from backend.deployment import ManagedFileDeployment
from backend.library.service import LuaLibraryService
from backend.runtime import BackendRuntime
from backend.storage import AppPaths


def _runtime(tmp_path: Path) -> BackendRuntime:
    paths = AppPaths.discover(
        development_root=tmp_path / 'data',
        resource_root=tmp_path / 'resources',
    )
    paths.ensure_writable_dirs()
    return BackendRuntime.from_paths(paths, legacy_root=tmp_path / 'legacy')


def _service(
    tmp_path: Path,
    *,
    config: dict[str, object] | None = None,
    steam_path: Path | None = None,
    lua_dir: Path | None = None,
    ost_ready: bool = True,
) -> tuple[LuaLibraryService, BackendRuntime, Path]:
    runtime = _runtime(tmp_path)
    steam = steam_path if steam_path is not None else tmp_path / 'Steam'
    library = lua_dir if lua_dir is not None else steam / 'config' / 'lua'

    async def no_lua(_appid: str) -> str | None:
        return None

    return (
        LuaLibraryService(
            runtime=runtime,
            steam_path=steam,
            lua_dir=library,
            config=config or {},
            fetch_ryuu_lua=no_lua,
            build_sudama_lua=no_lua,
            ost_available=lambda _path: ost_ready,
        ),
        runtime,
        library,
    )


def test_write_lua_validates_and_records_ownership(tmp_path: Path) -> None:
    service, runtime, library = _service(tmp_path)

    path = asyncio.run(service.write_lua('480', 'addappid(480)\n'))

    assert path == library / '480.lua'
    assert path.read_text(encoding='utf-8') == 'addappid(480)\n'
    status = ManagedFileDeployment(
        runtime.state_dir / 'library',
        'lua-480',
        library,
    ).status()
    assert status['managed'] is True
    assert status['healthy'] is True
    assert not list(runtime.temp_dir.glob('lua-stage-*'))


def test_install_game_falls_back_from_ryuu_to_sudama(tmp_path: Path) -> None:
    service, _runtime_value, library = _service(tmp_path)
    attempts: list[tuple[str, str]] = []

    async def ryuu(appid: str) -> str | None:
        attempts.append(('ryuu', appid))
        return None

    async def sudama(appid: str) -> str | None:
        attempts.append(('sudama', appid))
        return f'addappid({appid})\n'

    service.fetch_ryuu_lua = ryuu
    service.build_sudama_lua = sudama

    result = asyncio.run(service.install_game(' 480 ', source='auto'))

    assert result == (True, '入庫成功（來源: sudama）')
    assert attempts == [('ryuu', '480'), ('sudama', '480')]
    assert (library / '480.lua').is_file()


def test_explicit_ryuu_respects_disabled_auto_fallback(tmp_path: Path) -> None:
    service, _runtime_value, _library = _service(
        tmp_path,
        config={'auto_fallback': False},
    )
    attempts: list[str] = []

    async def ryuu(_appid: str) -> str | None:
        attempts.append('ryuu')
        return None

    async def sudama(_appid: str) -> str | None:
        attempts.append('sudama')
        return 'addappid(480)\n'

    service.fetch_ryuu_lua = ryuu
    service.build_sudama_lua = sudama

    result = asyncio.run(service.install_game('480', source='ryuu'))

    assert result == (False, 'ryuu 未提供資料')
    assert attempts == ['ryuu']


def test_install_game_preserves_validation_and_precondition_messages(
    tmp_path: Path,
) -> None:
    service, _runtime_value, _library = _service(tmp_path, ost_ready=False)

    assert asyncio.run(service.install_game('../480')) == (
        False,
        'AppID 必須為純數字',
    )
    assert asyncio.run(service.install_game('480')) == (
        False,
        '尚未安裝或偵測到 OpenSteamTool，請先完成 OST 安裝',
    )

    service.lua_dir = None
    assert asyncio.run(service.install_game('480')) == (False, '後端未初始化')


def test_list_installed_sorts_newest_first_and_reports_ownership(
    tmp_path: Path,
) -> None:
    service, _runtime_value, library = _service(tmp_path)
    managed = asyncio.run(service.write_lua('100', 'addappid(100)\n'))
    unmanaged = library / '200.lua'
    unmanaged.write_text('addappid(200)\n', encoding='utf-8')
    (library / 'readme.lua').write_text('-- ignored\n', encoding='utf-8')
    os.utime(managed, (1_000, 1_000))
    os.utime(unmanaged, (2_000, 2_000))

    rows = service.list_installed()

    assert [row['appid'] for row in rows] == ['200', '100']
    assert [row['managed'] for row in rows] == [False, True]
    assert rows[0]['path'] == str(unmanaged)


def test_remove_installed_handles_managed_legacy_and_invalid_ids(
    tmp_path: Path,
) -> None:
    service, _runtime_value, library = _service(tmp_path)
    managed = asyncio.run(service.write_lua('300', 'addappid(300)\n'))
    legacy = library / '400.lua'
    legacy.write_text('addappid(400)\n', encoding='utf-8')

    assert service.remove_installed('300') is True
    assert not managed.exists()
    assert service.remove_installed('400') is True
    assert not legacy.exists()
    assert service.remove_installed('999') is True
    assert service.remove_installed('１２３') is False
