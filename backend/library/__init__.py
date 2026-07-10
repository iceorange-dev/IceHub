"""Composable library-source service for the legacy backend facade."""

from __future__ import annotations

from collections.abc import Mapping
import logging
import time
from typing import Dict, Optional

from ..runtime import BackendRuntime
from .service import LuaLibraryService, LuaSource, OstAvailability
from .sources import Clock, HttpGet, RyuuSource, SteamCmdSource, SudamaSource


class LibrarySourceService:
    """Compose SteamCMD, Sudama, and Ryuu behind the four legacy methods."""

    def __init__(
        self,
        *,
        runtime: BackendRuntime,
        config: Mapping[str, object],
        http_get: HttpGet,
        logger: logging.Logger | None = None,
        clock: Clock = time.time,
    ) -> None:
        active_logger = logger or runtime.log
        self.steamcmd = SteamCmdSource(
            http_get=http_get,
            logger=active_logger,
        )
        self.sudama = SudamaSource(
            runtime=runtime,
            config=config,
            http_get=http_get,
            logger=active_logger,
            depot_source=self.steamcmd,
            clock=clock,
        )
        self.ryuu = RyuuSource(
            config=config,
            http_get=http_get,
            logger=active_logger,
        )

    async def get_depot_manifest_map(self, appid: str) -> Dict[str, str]:
        return await self.steamcmd.get_depot_manifest_map(appid)

    async def get_sudama_keys(self) -> Dict[str, str]:
        return await self.sudama.get_sudama_keys()

    async def fetch_ryuu_lua(self, appid: str) -> Optional[str]:
        return await self.ryuu.fetch_ryuu_lua(appid)

    async def build_lua_from_sudama(self, appid: str) -> Optional[str]:
        return await self.sudama.build_lua_from_sudama(appid)

    @property
    def sudama_cache(self) -> Optional[Dict[str, str]]:
        return self.sudama.cache

    @property
    def sudama_cache_timestamp(self) -> float:
        return self.sudama.cache_timestamp


# Short alias for composition roots that prefer the domain name.
LibrarySources = LibrarySourceService


__all__ = [
    'LibrarySourceService',
    'LibrarySources',
    'LuaLibraryService',
    'LuaSource',
    'OstAvailability',
]
