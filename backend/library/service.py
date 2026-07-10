"""Ownership-aware service for installing and managing Steam Lua entries."""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
import os
from pathlib import Path
from typing import Any, Protocol
import shutil
import uuid

from ..constants import MAX_LUA_BYTES
from ..deployment import ManagedFileDeployment
from ..runtime import BackendRuntime
from ..security import SecurityValidationError, sha256_file, validate_remote_lua


class LuaSource(Protocol):
    """Asynchronous source capable of returning validated-candidate Lua text."""

    def __call__(self, appid: str) -> Awaitable[str | None]: ...


class OstAvailability(Protocol):
    """Checks whether the initialized Steam directory can consume Lua files."""

    def __call__(self, steam_path: Path) -> bool: ...


class LuaLibraryService:
    """Install, enumerate, and remove ownership-tracked Lua library entries."""

    def __init__(
        self,
        *,
        runtime: BackendRuntime,
        steam_path: Path | None,
        lua_dir: Path | None,
        config: Mapping[str, Any],
        fetch_ryuu_lua: LuaSource,
        build_sudama_lua: LuaSource,
        ost_available: OstAvailability,
    ) -> None:
        self.runtime = runtime
        self.steam_path = Path(steam_path) if steam_path is not None else None
        self.lua_dir = Path(lua_dir) if lua_dir is not None else None
        self.config = config
        self.fetch_ryuu_lua = fetch_ryuu_lua
        self.build_sudama_lua = build_sudama_lua
        self.ost_available = ost_available
        self.log = runtime.log

    async def write_lua(self, appid: str, lua_content: str) -> Path:
        """Validate and transactionally publish one ``<appid>.lua`` file."""

        if self.lua_dir is None:
            raise RuntimeError('lua_dir 未初始化')
        self.lua_dir.mkdir(parents=True, exist_ok=True)
        appid = str(appid)
        lua_content = validate_remote_lua(
            lua_content,
            expected_appid=appid,
        )
        if len(lua_content.encode('utf-8')) > MAX_LUA_BYTES:
            raise SecurityValidationError('Lua 檔案超過大小上限')

        path = self.lua_dir / f'{appid}.lua'
        staging_dir = (
            self.runtime.temp_dir / f'lua-stage-{appid}-{uuid.uuid4().hex}'
        )
        staging_dir.mkdir(parents=True, exist_ok=False)
        staged = staging_dir / path.name
        try:
            with staged.open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(lua_content)
                stream.flush()
                os.fsync(stream.fileno())
            manager = ManagedFileDeployment(
                self.runtime.state_dir / 'library',
                f'lua-{appid}',
                self.lua_dir,
            )
            result = manager.install(
                {path.name: staged},
                version=sha256_file(staged)[:16],
            )
            path = result.changed_files[0]
        finally:
            shutil.rmtree(staging_dir, ignore_errors=True)
        self.log.info('已寫入 lua: %s', path)
        return path

    async def install_game(
        self,
        appid: str,
        source: str = 'auto',
    ) -> tuple[bool, str]:
        """Install from Ryuu, Sudama, or the configured fallback sequence."""

        appid = str(appid).strip()
        if not appid.isdigit():
            return False, 'AppID 必須為純數字'
        if self.steam_path is None or self.lua_dir is None:
            return False, '後端未初始化'
        if not self.ost_available(self.steam_path):
            return False, '尚未安裝或偵測到 OpenSteamTool，請先完成 OST 安裝'
        if source not in {'auto', 'ryuu', 'sudama'}:
            return False, f'不支援的入庫來源: {source}'

        if source == 'ryuu':
            order = ['ryuu']
            if self.config.get('auto_fallback', True):
                order.append('sudama')
        elif source == 'sudama':
            order = ['sudama']
        else:
            order = ['ryuu', 'sudama']

        last_error = ''
        for candidate in order:
            self.log.info('嘗試源: %s', candidate)
            try:
                if candidate == 'ryuu':
                    lua = await self.fetch_ryuu_lua(appid)
                else:
                    lua = await self.build_sudama_lua(appid)
                if lua:
                    await self.write_lua(appid, lua)
                    return True, f'入庫成功（來源: {candidate}）'
                last_error = f'{candidate} 未提供資料'
            except Exception as error:
                last_error = f'{candidate} 失敗: {error}'
                self.log.error(last_error)
        return False, last_error or '所有來源均失敗'

    def list_installed(self) -> list[dict[str, Any]]:
        """Return numeric Lua entries ordered by descending modification time."""

        if self.lua_dir is None or not self.lua_dir.exists():
            return []
        results: list[dict[str, Any]] = []
        for path in self.lua_dir.glob('*.lua'):
            if path.stem.isdigit():
                manager = ManagedFileDeployment(
                    self.runtime.state_dir / 'library',
                    f'lua-{path.stem}',
                    self.lua_dir,
                )
                results.append({
                    'appid': path.stem,
                    'path': str(path),
                    'mtime': path.stat().st_mtime,
                    'managed': manager.manifest_path.exists(),
                })
        return sorted(results, key=lambda item: -item['mtime'])

    def remove_installed(self, appid: str) -> bool:
        """Safely remove a managed or explicitly adopted legacy Lua entry."""

        if self.lua_dir is None:
            return False
        appid = str(appid).strip()
        if not appid.isascii() or not appid.isdigit():
            return False
        path = self.lua_dir / f'{appid}.lua'
        try:
            if not path.exists():
                return True
            manager = ManagedFileDeployment(
                self.runtime.state_dir / 'library',
                f'lua-{appid}',
                self.lua_dir,
            )
            if not manager.manifest_path.exists():
                manager.install(
                    {path.name: path},
                    version='legacy-migration',
                    created_targets={path.name},
                )
            manager.uninstall()
            return True
        except Exception as error:
            self.log.error('刪除 %s 失敗: %s', path, error)
            return False


__all__ = ['LuaLibraryService', 'LuaSource', 'OstAvailability']
