from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from backend import tool_updates
from backend.runtime import BackendRuntime
from backend.storage import AppPaths
from backend.tool_updates import ToolUpdateService


def _runtime(tmp_path: Path) -> BackendRuntime:
    paths = AppPaths.discover(
        development_root=tmp_path / 'data',
        resource_root=tmp_path / 'resources',
    )
    return BackendRuntime.from_paths(paths, legacy_root=tmp_path / 'legacy')


@asynccontextmanager
async def _unused_client_scope():
    raise AssertionError('this workflow must not open an HTTP client')
    yield


@pytest.mark.asyncio
async def test_ost_update_rejects_missing_steam_path_before_network(tmp_path: Path) -> None:
    service = ToolUpdateService(_runtime(tmp_path), {}, _unused_client_scope)

    result = await service.install_or_update_ost(None)

    assert result == (False, '未配置 Steam 路徑，請先在設定中指定')


@pytest.mark.asyncio
async def test_ost_update_delegates_download_install_and_version_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    config: dict[str, str] = {}
    service = ToolUpdateService(
        runtime,
        config,
        _unused_client_scope,
        steam_running=lambda: False,
    )
    steam_path = tmp_path / 'Steam'
    steam_path.mkdir()
    archive = tmp_path / 'OpenSteamTool.Release.zip'
    archive.write_bytes(b'archive')
    release = {
        'tag_name': 'v9.9.9',
        'assets': [
            {
                'name': archive.name,
                'size': archive.stat().st_size,
                'state': 'uploaded',
                'digest': 'sha256:' + ('a' * 64),
                'browser_download_url': 'https://github.com/example/archive.zip',
            }
        ],
    }
    stages: list[tuple[str, int, int]] = []
    config_updates: list[dict[str, str]] = []

    async def latest():
        return release

    async def downloaded(_asset, _progress):
        return archive

    def installed(path, target, *, version='', runtime=None):
        assert path == archive
        assert target == steam_path
        assert version == 'v9.9.9'
        return True, 'ok', [steam_path / 'OpenSteamTool.dll']

    monkeypatch.setattr(service, 'fetch_ost_latest', latest)
    monkeypatch.setattr(service, '_download_asset', downloaded)
    monkeypatch.setattr(tool_updates, 'install_ost_from_zip', installed)
    monkeypatch.setattr(
        tool_updates,
        'update_config_fields',
        lambda fields, *, runtime=None: config_updates.append(fields) or fields,
    )

    result = await service.install_or_update_ost(
        steam_path,
        lambda stage, current, total: stages.append((stage, current, total)),
    )

    assert result == (True, 'OST v9.9.9 安裝完成，共部署 1 個檔案')
    assert config['installed_ost_version'] == 'v9.9.9'
    assert config_updates == [{'installed_ost_version': 'v9.9.9'}]
    assert stages == [('fetch', 0, 0), ('install', 0, 1), ('install', 1, 1)]
    assert not archive.exists()


def test_remove_ost_stops_while_steam_is_running(tmp_path: Path) -> None:
    service = ToolUpdateService(
        _runtime(tmp_path),
        {},
        _unused_client_scope,
        steam_running=lambda: True,
    )

    result = service.remove_ost(tmp_path / 'Steam')

    assert result == (
        False,
        'Steam 仍在執行，請先完全關閉 Steam 後重試',
        [],
    )
