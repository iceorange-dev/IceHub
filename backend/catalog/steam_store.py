"""Steam Store search and metadata catalog."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import re
from typing import Any
import urllib.parse

import httpx

from ..cache import (
    get_cached_header,
    get_cached_name,
    set_cached_header,
    set_cached_name,
)
from ..runtime import BackendRuntime


HttpGet = Callable[..., Awaitable[httpx.Response]]


class SteamStoreCatalog:
    """Steam Store-backed search and game metadata service."""

    _MATURE_COOKIES = {
        'birthtime': '631152000',
        'lastagecheckage': '1-January-1980',
        'mature_content': '1',
        'wants_mature_content': '1',
    }

    def __init__(
        self,
        *,
        runtime: BackendRuntime,
        http_get: HttpGet,
        logger: logging.Logger,
    ) -> None:
        self.runtime = runtime
        self._http_get = http_get
        self.log = logger

    async def search_games(
        self,
        query: str,
        lang: str = 'tchinese',
    ) -> list[dict[str, Any]]:
        """Steam 商店搜尋（含成人內容繞過）"""

        del lang  # Kept for the existing public method contract.
        query = query.strip()
        if not query:
            return []

        # 純數字 → 視為 AppID 直接查
        if query.isdigit():
            info = await self.get_app_info(query)
            if info.get('name'):
                return [info]
            return [
                {
                    'appid': query,
                    'name': f'AppID {query}',
                    'header_image': (
                        'https://cdn.cloudflare.steamstatic.com/steam/apps/'
                        f'{query}/header.jpg'
                    ),
                }
            ]

        try:
            encoded = urllib.parse.quote(query)
            # category1=998,994 = 遊戲 + DLC 母集合；ndl=1 = 過濾 DLC 本身
            url = (
                'https://store.steampowered.com/search/'
                f'?term={encoded}&category1=998,994&ndl=1'
            )
            response = await self._http_get(
                url,
                cookies=self._MATURE_COOKIES,
                timeout=20,
            )
            if response.status_code != 200:
                self.log.warning(
                    f'Steam 搜尋失敗 HTTP {response.status_code}'
                )
                return []
            return self._parse_search_html(response.text)
        except Exception as error:
            self.log.error(f'搜尋出錯: {error}')
            return []

    def _parse_search_html(self, html: str) -> list[dict[str, Any]]:
        """從 Steam 搜尋結果 HTML 抽出 appid、title 與真實 header URL。"""

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        row_pattern = re.compile(
            r'<a[^>]*?href="https://store\.steampowered\.com/app/(\d+)/([^"/]*)/[^\"]*"[^>]*?data-ds-appid="\1"(.*?)</a>',
            re.DOTALL,
        )
        for appid, slug, content in row_pattern.findall(html):
            if appid in seen:
                continue
            seen.add(appid)
            match = re.search(r'<span class="title">(.*?)</span>', content)
            if match:
                name = match.group(1).strip()
            else:
                name = (
                    urllib.parse.unquote(slug).replace('_', ' ').strip()
                    or f'AppID {appid}'
                )
            header_url = self._extract_header_from_row(content, appid)
            results.append(
                {
                    'appid': appid,
                    'name': name,
                    'header_image': header_url,
                }
            )
            if header_url and 'store_item_assets' in header_url:
                set_cached_header(
                    appid,
                    header_url,
                    runtime=self.runtime,
                )
            if len(results) >= 30:
                break

        # 舊版相容：部分結果不含 data-ds-appid。
        if not results:
            simple = re.compile(
                r'<a href="https://store\.steampowered\.com/app/(\d+)/([^/]*)/[^\"]*"[^>]*>(.*?)</a>',
                re.DOTALL,
            )
            for appid, slug, content in simple.findall(html):
                if appid in seen:
                    continue
                seen.add(appid)
                match = re.search(
                    r'<span class="title">(.*?)</span>',
                    content,
                )
                name = (
                    match.group(1).strip()
                    if match
                    else urllib.parse.unquote(slug).replace('_', ' ')
                )
                header_url = self._extract_header_from_row(content, appid)
                results.append(
                    {
                        'appid': appid,
                        'name': name or f'AppID {appid}',
                        'header_image': header_url,
                    }
                )
                if len(results) >= 30:
                    break
        return results

    def _extract_header_from_row(self, row_html: str, appid: str) -> str:
        """Extract and upgrade the image URL for one Steam search row."""

        match = re.search(
            r'src="(https://[^"]+/(?:capsule_\d+x\d+|header)\.jpg[^"]*)"',
            row_html,
        )
        if match:
            url = match.group(1)
            return re.sub(r'capsule_\d+x\d+\.jpg', 'header.jpg', url)
        fallback = re.search(r'src="(https://[^"]+\.jpg[^"]*)"', row_html)
        if fallback:
            return fallback.group(1)
        return (
            'https://cdn.cloudflare.steamstatic.com/steam/apps/'
            f'{appid}/header.jpg'
        )

    async def fetch_recommendations(self) -> list[dict[str, Any]]:
        """取得熱門遊戲列表"""

        try:
            response = await self._http_get(
                'https://store.steampowered.com/api/featuredcategories',
                params={'cc': 'tw', 'l': 'tchinese'},
                timeout=15,
            )
            data = response.json()
            games: list[dict[str, Any]] = []
            seen: set[str] = set()
            for section in ['top_sellers', 'new_releases', 'specials']:
                items = data.get(section, {}).get('items', [])
                for item in items:
                    appid = str(item.get('id', ''))
                    name = item.get('name', '')
                    if appid and name and appid not in seen:
                        seen.add(appid)
                        games.append(
                            {
                                'appid': appid,
                                'name': name,
                                'header_image': item.get('header_image')
                                or (
                                    'https://cdn.cloudflare.steamstatic.com/'
                                    f'steam/apps/{appid}/header.jpg'
                                ),
                            }
                        )
            return games[:30]
        except Exception as error:
            self.log.error(f'取得推薦失敗: {error}')
            return []

    async def get_app_info(self, appid: str) -> dict[str, Any]:
        """取得遊戲基本資訊（含本地快取與真實 header URL）。"""

        appid = str(appid)
        cached_name = get_cached_name(appid, runtime=self.runtime)
        cached_header = get_cached_header(appid, runtime=self.runtime)
        try:
            response = await self._http_get(
                'https://store.steampowered.com/api/appdetails'
                f'?appids={appid}&l=tchinese',
                timeout=15,
                cookies=self._MATURE_COOKIES,
            )
            if response.status_code == 200:
                data = response.json().get(appid, {})
                if data.get('success'):
                    details = data.get('data', {})
                    name = details.get('name', '') or cached_name
                    header_image = (
                        details.get('header_image', '') or cached_header
                    )
                    if name:
                        set_cached_name(appid, name, runtime=self.runtime)
                    if header_image:
                        set_cached_header(
                            appid,
                            header_image,
                            runtime=self.runtime,
                        )
                    return {
                        'appid': appid,
                        'name': name,
                        'header_image': header_image
                        or (
                            'https://cdn.cloudflare.steamstatic.com/'
                            f'steam/apps/{appid}/header.jpg'
                        ),
                        'description': details.get('short_description', ''),
                    }
        except Exception:
            pass
        return {
            'appid': appid,
            'name': cached_name,
            'header_image': cached_header
            or (
                'https://cdn.cloudflare.steamstatic.com/steam/apps/'
                f'{appid}/header.jpg'
            ),
        }

    async def fetch_names_for(
        self,
        appids: list[str],
    ) -> dict[str, dict[str, str]]:
        """批次取得多個 appid 的名稱與 header URL。"""

        result: dict[str, dict[str, str]] = {}
        need_fetch: list[str] = []
        for appid in appids:
            appid = str(appid)
            cached_name = get_cached_name(appid, runtime=self.runtime)
            cached_header = get_cached_header(appid, runtime=self.runtime)
            if cached_name and cached_header:
                result[appid] = {
                    'name': cached_name,
                    'header_image': cached_header,
                }
            else:
                need_fetch.append(appid)
        if need_fetch:
            semaphore = asyncio.Semaphore(5)

            async def one(appid: str) -> tuple[str, dict[str, Any]]:
                async with semaphore:
                    info = await self.get_app_info(appid)
                    return appid, info

            tasks = [one(appid) for appid in need_fetch]
            for coroutine in asyncio.as_completed(tasks):
                try:
                    appid, info = await coroutine
                    if info.get('name') or info.get('header_image'):
                        result[appid] = {
                            'name': info.get('name', ''),
                            'header_image': info.get('header_image', ''),
                        }
                except Exception:
                    pass
        return result


__all__ = ['HttpGet', 'SteamStoreCatalog']
