"""Sudama depot-key cache, download, and Lua assembly source."""

from __future__ import annotations

from collections.abc import Mapping
import json
import logging
import time
from typing import Dict, Optional, Protocol

from ...constants import DEFAULT_CONFIG, MAX_SUDAMA_BYTES
from ...http import read_response_limited, validated_https_url
from ...runtime import BackendRuntime
from ...security import (
    SecurityValidationError,
    validate_depot_key,
    validate_manifest_gid,
    validate_remote_lua,
)
from ...storage import CorruptJsonError
from ._types import Clock, HttpGet


class DepotManifestSource(Protocol):
    async def get_depot_manifest_map(self, appid: str) -> Dict[str, str]: ...


class SudamaSource:
    """Own the bounded Sudama key cache and build validated Lua from it."""

    def __init__(
        self,
        *,
        runtime: BackendRuntime,
        config: Mapping[str, object],
        http_get: HttpGet,
        logger: logging.Logger,
        depot_source: DepotManifestSource,
        clock: Clock = time.time,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self._http_get = http_get
        self.log = logger
        self.depot_source = depot_source
        self._clock = clock
        self._sudama_cache: Optional[Dict[str, str]] = None
        self._sudama_cache_ts = 0.0

    @property
    def cache(self) -> Optional[Dict[str, str]]:
        """Expose the current snapshot for facade migration and diagnostics."""

        return self._sudama_cache

    @property
    def cache_timestamp(self) -> float:
        return self._sudama_cache_ts

    def _sanitize(self, raw: object) -> Dict[str, str]:
        if not isinstance(raw, dict):
            raise SecurityValidationError('Sudama 回應必須是 JSON object')
        sanitized: Dict[str, str] = {}
        rejected = 0
        for raw_depot, raw_key in raw.items():
            depot_id = str(raw_depot)
            try:
                if not depot_id.isascii() or not depot_id.isdigit():
                    raise SecurityValidationError('depot id 必須為數字')
                sanitized[depot_id] = validate_depot_key(str(raw_key).strip())
            except SecurityValidationError:
                rejected += 1
        if rejected:
            self.log.warning('Sudama 資料中忽略 %s 筆格式無效紀錄', rejected)
        return sanitized

    async def get_sudama_keys(self) -> Dict[str, str]:
        """取得 Sudama depot key 全量資料庫，含記憶體與磁碟快取。"""

        cache_hours = self.config.get('Sudama_Cache_Hours', 24)
        if not isinstance(cache_hours, (int, float)) or isinstance(cache_hours, bool):
            cache_hours = 24
        cache_hours = max(0, min(float(cache_hours), 24 * 30))
        max_age = cache_hours * 3600
        now = self._clock()

        if self._sudama_cache and (now - self._sudama_cache_ts) < max_age:
            return self._sudama_cache

        try:
            data = self.runtime.sudama_store.read()
            if isinstance(data, dict):
                timestamp = float(data.get('ts', 0) or 0)
                if timestamp > 0 and (now - timestamp) < max_age:
                    self._sudama_cache = self._sanitize(data.get('keys', {}))
                    self._sudama_cache_ts = timestamp
                    self.log.info(
                        '使用本地 Sudama 快取（%s 條）',
                        len(self._sudama_cache),
                    )
                    return self._sudama_cache
        except (
            OSError,
            ValueError,
            TypeError,
            CorruptJsonError,
            SecurityValidationError,
        ) as error:
            self.log.warning('忽略無效的 Sudama 快取: %s', error)

        try:
            url = self.config.get('Sudama_API', DEFAULT_CONFIG['Sudama_API'])
            validated_https_url(
                url,  # type: ignore[arg-type]
                allowed_hosts={'api.993499094.xyz'},
            )
            self.log.info('正在下載 Sudama 全量金鑰庫...')
            response = await self._http_get(
                url,
                timeout=60,
                follow_redirects=False,
            )
            response.raise_for_status()
            raw = await read_response_limited(
                response,
                max_bytes=MAX_SUDAMA_BYTES,
            )
            data = json.loads(raw.decode('utf-8'))
            self._sudama_cache = self._sanitize(data)
            self._sudama_cache_ts = self._clock()
            try:
                self.runtime.sudama_store.write(
                    {
                        'keys': self._sudama_cache,
                        'ts': self._sudama_cache_ts,
                    }
                )
            except (OSError, CorruptJsonError) as error:
                self.log.warning('Sudama 快取寫入失敗: %s', error)
            self.log.info(
                'Sudama 金鑰庫下載完成，共 %s 條',
                len(self._sudama_cache),
            )
            return self._sudama_cache
        except Exception as error:
            self.log.error('下載 Sudama 失敗: %s', error)
            return {}

    async def build_lua_from_sudama(self, appid: str) -> Optional[str]:
        """組合 SteamCMD depot manifest 與 Sudama key，產生安全 Lua。"""

        depot_map = await self.depot_source.get_depot_manifest_map(appid)
        if not depot_map:
            self.log.warning('未能獲取 AppID %s 的 depot 列表', appid)
            return None
        keys = await self.get_sudama_keys()
        if not keys:
            return None

        lines = [f'addappid({appid})']
        matched = 0
        for depot_id, gid in depot_map.items():
            try:
                if not str(depot_id).isascii() or not str(depot_id).isdigit():
                    raise SecurityValidationError('depot id 必須為數字')
                gid = validate_manifest_gid(gid)
            except SecurityValidationError as error:
                self.log.error('Sudama manifest 資料無效: %s', error)
                return None
            key = keys.get(depot_id, '').strip()
            if key:
                try:
                    key = validate_depot_key(key)
                except SecurityValidationError as error:
                    self.log.error('Sudama depot key 無效: %s', error)
                    return None
                lines.append(f'addappid({depot_id}, 1, "{key}")')
                matched += 1
            else:
                lines.append(f'addappid({depot_id})')
            lines.append(f'setManifestid({depot_id}, "{gid}")')

        if matched == 0:
            self.log.warning('AppID %s 在 Sudama 中沒有任何 depot 金鑰', appid)
            return None
        self.log.info('Sudama 匹配到 %s/%s 個 depot 金鑰', matched, len(depot_map))
        lua = '\n'.join(lines) + '\n'
        try:
            return validate_remote_lua(lua, expected_appid=appid)
        except SecurityValidationError as error:
            self.log.error('產生的 Sudama Lua 未通過驗證: %s', error)
            return None


__all__ = ['DepotManifestSource', 'SudamaSource']
