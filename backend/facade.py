"""Application-facing backend composition root.

``OSTBackend`` intentionally contains only lifecycle, initialization, and
delegation.  Catalog lookup, library sources, Lua ownership, installers, and
release updates live in their own domain modules.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx

from .catalog import SteamStoreCatalog
from .config import load_config, update_config_fields
from .deployment import DeploymentError, ManagedFileDeployment, OwnershipError
from .installers.ost import ost_installed
from .installers.steamless import _read_steamless_internal_manifest
from .library import LibrarySourceService, LuaLibraryService
from .platform.files import _is_link_or_reparse
from .platform.steam import detect_steam_path, is_steam_running
from .runtime import BackendRuntime, resolve_runtime
from .tool_updates import ProgressCallback, ToolUpdateService


SteamPathDetector = Callable[[str], Path | None]
SteamRunningChecker = Callable[[], bool]


class OSTBackend:
    """Thin compatibility facade composed from focused backend services."""

    _DEFAULT_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36'
        ),
        'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
    }

    def __init__(
        self,
        *,
        runtime: BackendRuntime | None = None,
        config: dict[str, Any] | None = None,
        client: httpx.AsyncClient | None = None,
        steam_path_detector: SteamPathDetector = detect_steam_path,
        steam_running_checker: SteamRunningChecker = is_steam_running,
    ) -> None:
        self.runtime = resolve_runtime(runtime)
        self.config: dict[str, Any] = (
            dict(config)
            if config is not None
            else load_config(runtime=self.runtime)
        )
        self.log = self.runtime.log
        self.steam_path: Path | None = None
        self.lua_dir: Path | None = None
        self._client = client
        self._owns_client = False
        self._steam_path_detector = steam_path_detector
        self._steam_running_checker = steam_running_checker

        self._catalog = SteamStoreCatalog(
            runtime=self.runtime,
            http_get=self._http_get,
            logger=self.log,
        )
        self._sources = LibrarySourceService(
            runtime=self.runtime,
            config=self.config,
            http_get=self._http_get,
            logger=self.log,
        )
        self._tool_updates = ToolUpdateService(
            self.runtime,
            self.config,
            self._client_scope,
            steam_running=self._steam_running_checker,
        )

    async def __aenter__(self) -> OSTBackend:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30,
                follow_redirects=True,
                headers=self._DEFAULT_HEADERS,
            )
            self._owns_client = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
            self._owns_client = False

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError(
                'No persistent HTTP client is active; use OSTBackend as an '
                'async context manager'
            )
        return self._client

    @asynccontextmanager
    async def _client_scope(self):
        """Reuse an injected client or own a client for this event loop."""

        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(
            timeout=30,
            follow_redirects=True,
            headers=self._DEFAULT_HEADERS,
        ) as client:
            yield client

    async def _http_get(self, *args, **kwargs) -> httpx.Response:
        async with self._client_scope() as client:
            return await client.get(*args, **kwargs)

    def _reconcile_installed_versions(self) -> None:
        """Refresh display-only version caches from ownership manifests."""

        ost_version = ''
        if self.steam_path:
            try:
                status = ManagedFileDeployment(
                    self.runtime.state_dir,
                    'ost',
                    self.steam_path,
                ).status()
                if status.get('managed'):
                    ost_version = str(status.get('version', ''))
            except DeploymentError as error:
                self.log.warning(
                    '無法讀取 OST ownership manifest: %s',
                    error,
                )

        steamless_version = ''
        steamless_dir = self.runtime.steamless_dir
        if steamless_dir.is_dir() and not _is_link_or_reparse(steamless_dir):
            try:
                manifest = _read_steamless_internal_manifest(
                    runtime=self.runtime,
                )
                if (
                    manifest.get('schema') == 1
                    and manifest.get('product') == 'steamless'
                ):
                    steamless_version = str(manifest.get('version', ''))
            except OwnershipError as error:
                self.log.warning(
                    '無法讀取 Steamless ownership manifest: %s',
                    error,
                )

        patch = {
            'installed_ost_version': ost_version,
            'installed_steamless_version': steamless_version,
        }
        if any(self.config.get(name, '') != value for name, value in patch.items()):
            try:
                self.config.update(
                    update_config_fields(patch, runtime=self.runtime)
                )
            except Exception as error:
                self.log.warning('無法同步已安裝版本快取: %s', error)

    def initialize(self) -> tuple[bool, str]:
        self.steam_path = self._steam_path_detector(
            str(self.config.get('Custom_Steam_Path', ''))
        )
        if not self.steam_path:
            return False, '未能自動獲取 Steam 路徑，請在設定中手動指定。'

        self.lua_dir = self.steam_path / 'config' / 'lua'
        try:
            self.lua_dir.mkdir(parents=True, exist_ok=True)
        except Exception as error:
            return False, f'無法建立 lua 目錄: {error}'

        self._reconcile_installed_versions()
        self.log.info('Steam 路徑: %s', self.steam_path)
        status = (
            '已安裝'
            if ost_installed(self.steam_path, runtime=self.runtime)
            else '未安裝'
        )
        self.log.info('OST 狀態: %s', status)
        return True, ''

    # Steam Store catalog -------------------------------------------------
    async def search_games(
        self,
        query: str,
        lang: str = 'tchinese',
    ) -> list[dict[str, Any]]:
        return await self._catalog.search_games(query, lang)

    def _parse_search_html(self, html: str) -> list[dict[str, Any]]:
        return self._catalog._parse_search_html(html)

    def _extract_header_from_row(self, row_html: str, appid: str) -> str:
        return self._catalog._extract_header_from_row(row_html, appid)

    async def fetch_recommendations(self) -> list[dict[str, Any]]:
        return await self._catalog.fetch_recommendations()

    async def get_app_info(self, appid: str) -> dict[str, Any]:
        return await self._catalog.get_app_info(appid)

    async def fetch_names_for(
        self,
        appids: list[str],
    ) -> dict[str, dict[str, str]]:
        return await self._catalog.fetch_names_for(appids)

    # Ryuu, Sudama, and SteamCMD sources ----------------------------------
    async def get_depot_manifest_map(self, appid: str) -> dict[str, str]:
        return await self._sources.get_depot_manifest_map(appid)

    async def get_sudama_keys(self) -> dict[str, str]:
        return await self._sources.get_sudama_keys()

    async def fetch_ryuu_lua(self, appid: str) -> str | None:
        return await self._sources.fetch_ryuu_lua(appid)

    async def build_lua_from_sudama(self, appid: str) -> str | None:
        return await self._sources.build_lua_from_sudama(appid)

    @property
    def _sudama_cache(self) -> dict[str, str] | None:
        return self._sources.sudama_cache

    @property
    def _sudama_cache_ts(self) -> float:
        return self._sources.sudama_cache_timestamp

    # Ownership-aware Lua library ----------------------------------------
    def _library(self) -> LuaLibraryService:
        return LuaLibraryService(
            runtime=self.runtime,
            steam_path=self.steam_path,
            lua_dir=self.lua_dir,
            config=self.config,
            fetch_ryuu_lua=self.fetch_ryuu_lua,
            build_sudama_lua=self.build_lua_from_sudama,
            ost_available=lambda path: ost_installed(
                path,
                runtime=self.runtime,
            ),
        )

    async def write_lua(self, appid: str, lua_content: str) -> Path:
        return await self._library().write_lua(appid, lua_content)

    async def install_game(
        self,
        appid: str,
        source: str = 'auto',
    ) -> tuple[bool, str]:
        return await self._library().install_game(appid, source)

    def list_installed(self) -> list[dict[str, Any]]:
        return self._library().list_installed()

    def remove_installed(self, appid: str) -> bool:
        return self._library().remove_installed(appid)

    # Tool release updates ------------------------------------------------
    async def fetch_ost_latest(self) -> dict[str, Any] | None:
        return await self._tool_updates.fetch_ost_latest()

    async def install_or_update_ost(
        self,
        progress_cb: ProgressCallback | None = None,
    ) -> tuple[bool, str]:
        return await self._tool_updates.install_or_update_ost(
            self.steam_path,
            progress_cb,
        )

    def uninstall_ost(
        self,
        keep_installed_games: bool = True,
    ) -> tuple[bool, str, list[str]]:
        return self._tool_updates.remove_ost(
            self.steam_path,
            keep_installed_games,
        )

    async def fetch_steamless_latest(self) -> dict[str, Any] | None:
        return await self._tool_updates.fetch_steamless_latest()

    async def install_or_update_steamless(
        self,
        progress_cb: ProgressCallback | None = None,
    ) -> tuple[bool, str]:
        return await self._tool_updates.install_or_update_steamless(progress_cb)

    def uninstall_steamless(self) -> tuple[bool, str]:
        return self._tool_updates.remove_steamless()


__all__ = ['OSTBackend', 'SteamPathDetector', 'SteamRunningChecker']
