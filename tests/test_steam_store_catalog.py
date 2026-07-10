from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from backend.cache import get_cached_header, get_cached_name
from backend.catalog import SteamStoreCatalog
from backend.runtime import BackendRuntime
from backend.storage import AppPaths


def _runtime(tmp_path: Path) -> BackendRuntime:
    paths = AppPaths.discover(development_root=tmp_path / 'data')
    paths.ensure_writable_dirs()
    return BackendRuntime.from_paths(paths)


def _search_row(appid: str = '480', name: str = 'Spacewar') -> str:
    return (
        f'<a href="https://store.steampowered.com/app/{appid}/{name}/" '
        f'data-ds-appid="{appid}">'
        '<img src="https://shared.test/store_item_assets/steam/apps/'
        f'{appid}/capsule_231x87.jpg?t=1">'
        f'<span class="title">{name}</span>'
        '</a>'
    )


def test_search_uses_mature_cookies_and_persists_real_header(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    calls: list[tuple[str, dict]] = []

    async def get(url: str, **kwargs) -> httpx.Response:
        calls.append((url, kwargs))
        return httpx.Response(200, text=_search_row())

    catalog = SteamStoreCatalog(
        runtime=runtime,
        http_get=get,
        logger=logging.getLogger('IceHub.catalog-test'),
    )
    results = asyncio.run(catalog.search_games('  Half Life  '))

    assert results == [
        {
            'appid': '480',
            'name': 'Spacewar',
            'header_image': (
                'https://shared.test/store_item_assets/steam/apps/'
                '480/header.jpg?t=1'
            ),
        }
    ]
    assert 'term=Half%20Life' in calls[0][0]
    assert calls[0][1]['cookies']['wants_mature_content'] == '1'
    assert get_cached_header('480', runtime=runtime).endswith('header.jpg?t=1')


def test_numeric_search_preserves_direct_appid_fallback(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    async def get(_url: str, **_kwargs) -> httpx.Response:
        return httpx.Response(503)

    catalog = SteamStoreCatalog(
        runtime=runtime,
        http_get=get,
        logger=logging.getLogger('IceHub.catalog-test'),
    )

    assert asyncio.run(catalog.search_games('480')) == [
        {
            'appid': '480',
            'name': 'AppID 480',
            'header_image': (
                'https://cdn.cloudflare.steamstatic.com/steam/apps/'
                '480/header.jpg'
            ),
        }
    ]


def test_app_details_update_cache_and_network_failure_uses_it(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    async def successful(_url: str, **_kwargs) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                '480': {
                    'success': True,
                    'data': {
                        'name': 'Spacewar',
                        'header_image': 'https://cdn.test/480.jpg',
                        'short_description': 'A test game',
                    },
                }
            },
        )

    catalog = SteamStoreCatalog(
        runtime=runtime,
        http_get=successful,
        logger=logging.getLogger('IceHub.catalog-test'),
    )
    info = asyncio.run(catalog.get_app_info('480'))

    assert info['description'] == 'A test game'
    assert get_cached_name('480', runtime=runtime) == 'Spacewar'
    assert get_cached_header('480', runtime=runtime) == 'https://cdn.test/480.jpg'

    async def failing(_url: str, **_kwargs) -> httpx.Response:
        raise httpx.ConnectError('offline')

    offline = SteamStoreCatalog(
        runtime=runtime,
        http_get=failing,
        logger=logging.getLogger('IceHub.catalog-test'),
    )
    assert asyncio.run(offline.get_app_info('480')) == {
        'appid': '480',
        'name': 'Spacewar',
        'header_image': 'https://cdn.test/480.jpg',
    }


def test_recommendations_keep_section_order_and_deduplicate(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    async def get(_url: str, **_kwargs) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                'top_sellers': {
                    'items': [
                        {'id': 10, 'name': 'Ten', 'header_image': 'ten.jpg'},
                    ]
                },
                'new_releases': {
                    'items': [
                        {'id': 20, 'name': 'Twenty', 'header_image': ''},
                    ]
                },
                'specials': {
                    'items': [
                        {'id': 10, 'name': 'Duplicate', 'header_image': 'dup.jpg'},
                    ]
                },
            },
        )

    catalog = SteamStoreCatalog(
        runtime=runtime,
        http_get=get,
        logger=logging.getLogger('IceHub.catalog-test'),
    )
    results = asyncio.run(catalog.fetch_recommendations())

    assert [item['appid'] for item in results] == ['10', '20']
    assert results[1]['header_image'].endswith('/20/header.jpg')


def test_fetch_names_combines_complete_cache_with_fetched_metadata(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.name_store.write({'480': 'Spacewar'})
    runtime.header_store.write({'480': 'https://cdn.test/480.jpg'})
    runtime.name_cache.update({'480': 'Spacewar'})
    runtime.header_cache.update({'480': 'https://cdn.test/480.jpg'})
    requested: list[str] = []

    async def get(url: str, **_kwargs) -> httpx.Response:
        requested.append(url)
        return httpx.Response(
            200,
            json={
                '570': {
                    'success': True,
                    'data': {
                        'name': 'Dota 2',
                        'header_image': 'https://cdn.test/570.jpg',
                    },
                }
            },
        )

    catalog = SteamStoreCatalog(
        runtime=runtime,
        http_get=get,
        logger=logging.getLogger('IceHub.catalog-test'),
    )
    result = asyncio.run(catalog.fetch_names_for(['480', '570']))

    assert result == {
        '480': {
            'name': 'Spacewar',
            'header_image': 'https://cdn.test/480.jpg',
        },
        '570': {
            'name': 'Dota 2',
            'header_image': 'https://cdn.test/570.jpg',
        },
    }
    assert len(requested) == 1
    assert 'appids=570' in requested[0]
