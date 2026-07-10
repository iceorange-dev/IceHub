"""SteamCMD depot manifest source."""

from __future__ import annotations

import logging
from typing import Dict

from ...security import SecurityValidationError, validate_manifest_gid
from ._types import HttpGet


class SteamCmdSource:
    """Fetch validated ``depot_id -> public manifest gid`` mappings."""

    def __init__(self, *, http_get: HttpGet, logger: logging.Logger) -> None:
        self._http_get = http_get
        self.log = logger

    async def get_depot_manifest_map(self, appid: str) -> Dict[str, str]:
        """從 SteamCMD API 取得 depot_id -> manifest_gid 對映。"""

        urls = [f'https://api.steamcmd.net/v1/info/{appid}']
        for url in urls:
            try:
                response = await self._http_get(
                    url,
                    headers={
                        'User-Agent': 'IceHub/1.0',
                        'Accept': 'application/json',
                    },
                    timeout=15,
                )
                if response.status_code != 200:
                    continue
                data = response.json()
                info = data.get('data', {}).get(str(appid), {})
                depots = info.get('depots', {})
                result: Dict[str, str] = {}
                if not depots:
                    continue
                for depot_id, depot_info in depots.items():
                    if not isinstance(depot_info, dict):
                        continue
                    if not depot_id.isdigit():
                        continue
                    manifests = depot_info.get('manifests', {}) or {}
                    public = (
                        manifests.get('public', {})
                        if isinstance(manifests.get('public'), dict)
                        else {}
                    )
                    gid = public.get('gid')
                    if gid:
                        try:
                            result[depot_id] = validate_manifest_gid(gid)
                        except SecurityValidationError:
                            self.log.warning(
                                '忽略格式無效的 manifest GID: depot %s',
                                depot_id,
                            )
                if result:
                    self.log.info(
                        '已取得 %s 個 depot 對映 (%s)',
                        len(result),
                        url.split('/')[2],
                    )
                    return result
            except Exception as error:
                self.log.debug('depot 對映取得失敗 (%s): %s', url, error)
        return {}


__all__ = ['SteamCmdSource']
