from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from typing import Any, Callable

import httpx

from backend.constants import MAX_LUA_BYTES, MAX_SUDAMA_BYTES
from backend.library import LibrarySourceService, LibrarySources
from backend.library.sources import RyuuSource, SteamCmdSource, SudamaSource
from backend.runtime import BackendRuntime
from backend.storage import AppPaths


VALID_KEY = '0123456789ABCDEF' * 4


class StubGet:
    def __init__(
        self,
        handler: Callable[[str, dict[str, Any]], httpx.Response],
    ) -> None:
        self.handler = handler
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append((url, kwargs))
        return self.handler(url, kwargs)


class DepotStub:
    def __init__(self, manifests: dict[str, str]) -> None:
        self.manifests = manifests
        self.calls: list[str] = []

    async def get_depot_manifest_map(self, appid: str) -> dict[str, str]:
        self.calls.append(appid)
        return dict(self.manifests)


def _runtime(tmp_path: Path) -> BackendRuntime:
    paths = AppPaths.discover(development_root=tmp_path / 'data')
    paths.ensure_writable_dirs()
    return BackendRuntime.from_paths(paths)


def _json_response(url: str, value: object, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json=value,
        request=httpx.Request('GET', url),
    )


def _bytes_response(
    url: str,
    content: bytes,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status,
        content=content,
        headers=headers,
        request=httpx.Request('GET', url),
    )


def test_steamcmd_source_validates_and_canonicalizes_manifest_gids() -> None:
    payload = {
        'data': {
            '480': {
                'depots': {
                    '100': {'manifests': {'public': {'gid': '000123'}}},
                    '200': {
                        'manifests': {
                            'public': {'gid': '18446744073709551616'},
                        }
                    },
                    'not-a-depot': {'manifests': {'public': {'gid': '7'}}},
                    '300': {'manifests': {'public': 'not-an-object'}},
                }
            }
        }
    }
    get = StubGet(lambda url, _kwargs: _json_response(url, payload))
    source = SteamCmdSource(http_get=get, logger=logging.getLogger('test.steamcmd'))

    result = asyncio.run(source.get_depot_manifest_map('480'))

    assert result == {'100': '123'}
    assert get.calls == [
        (
            'https://api.steamcmd.net/v1/info/480',
            {
                'headers': {
                    'User-Agent': 'IceHub/1.0',
                    'Accept': 'application/json',
                },
                'timeout': 15,
            },
        )
    ]


def test_steamcmd_source_returns_empty_mapping_on_http_or_schema_failure() -> None:
    get = StubGet(
        lambda url, _kwargs: _json_response(url, {'unexpected': True}, status=503)
    )
    source = SteamCmdSource(http_get=get, logger=logging.getLogger('test.steamcmd'))

    assert asyncio.run(source.get_depot_manifest_map('480')) == {}


def test_sudama_download_filters_keys_and_reuses_memory_and_disk_cache(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    payload = json.dumps(
        {
            '100': VALID_KEY,
            '200': 'not-a-key',
            'bad-depot': VALID_KEY,
        }
    ).encode()
    get = StubGet(lambda url, _kwargs: _bytes_response(url, payload))
    config = {
        'Sudama_API': 'https://api.993499094.xyz/depotkeys.json',
        'Sudama_Cache_Hours': 24,
    }
    source = SudamaSource(
        runtime=runtime,
        config=config,
        http_get=get,
        logger=logging.getLogger('test.sudama'),
        depot_source=DepotStub({}),
        clock=lambda: 1_000.0,
    )

    first = asyncio.run(source.get_sudama_keys())
    second = asyncio.run(source.get_sudama_keys())

    assert first == {'100': VALID_KEY.lower()}
    assert second is first
    assert len(get.calls) == 1
    assert get.calls[0][1] == {'timeout': 60, 'follow_redirects': False}
    assert runtime.sudama_store.read() == {
        'keys': first,
        'ts': 1_000.0,
    }

    def no_network(_url: str, _kwargs: dict[str, Any]) -> httpx.Response:
        raise AssertionError('fresh disk cache must avoid the network')

    disk_get = StubGet(no_network)
    fresh_process = SudamaSource(
        runtime=runtime,
        config=config,
        http_get=disk_get,
        logger=logging.getLogger('test.sudama.disk'),
        depot_source=DepotStub({}),
        clock=lambda: 1_001.0,
    )

    assert asyncio.run(fresh_process.get_sudama_keys()) == first
    assert disk_get.calls == []


def test_sudama_rejects_untrusted_endpoint_and_oversized_response(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)

    def must_not_run(_url: str, _kwargs: dict[str, Any]) -> httpx.Response:
        raise AssertionError('untrusted endpoint must be rejected before HTTP')

    blocked_get = StubGet(must_not_run)
    blocked = SudamaSource(
        runtime=runtime,
        config={
            'Sudama_API': 'https://attacker.test/depotkeys.json',
            'Sudama_Cache_Hours': 0,
        },
        http_get=blocked_get,
        logger=logging.getLogger('test.sudama.blocked'),
        depot_source=DepotStub({}),
    )
    assert asyncio.run(blocked.get_sudama_keys()) == {}
    assert blocked_get.calls == []

    oversized_get = StubGet(
        lambda url, _kwargs: _bytes_response(
            url,
            b'{}',
            headers={'Content-Length': str(MAX_SUDAMA_BYTES + 1)},
        )
    )
    oversized = SudamaSource(
        runtime=runtime,
        config={
            'Sudama_API': 'https://api.993499094.xyz/depotkeys.json',
            'Sudama_Cache_Hours': 0,
        },
        http_get=oversized_get,
        logger=logging.getLogger('test.sudama.oversized'),
        depot_source=DepotStub({}),
    )
    assert asyncio.run(oversized.get_sudama_keys()) == {}
    assert runtime.sudama_store.read() == {}


def test_ryuu_source_requires_trusted_endpoint_and_valid_data_only_lua() -> None:
    safe_lua = b'addappid(480)\n'
    get = StubGet(lambda url, _kwargs: _bytes_response(url, safe_lua))
    source = RyuuSource(
        config={
            'Ryuu_Auth_Key': 'secret',
            'Ryuu_API_Base': 'https://generator.ryuu.lol',
        },
        http_get=get,
        logger=logging.getLogger('test.ryuu'),
    )

    assert asyncio.run(source.fetch_ryuu_lua('480')) == safe_lua.decode()
    assert get.calls[0] == (
        'https://generator.ryuu.lol/api/download/480?file_type=lua',
        {
            'headers': {'X-Auth-Key': 'secret'},
            'timeout': 60,
            'follow_redirects': False,
        },
    )

    injection = StubGet(
        lambda url, _kwargs: _bytes_response(
            url,
            b'addappid(480)\nos.execute("calc")\n',
        )
    )
    unsafe = RyuuSource(
        config={'Ryuu_Auth_Key': 'secret'},
        http_get=injection,
        logger=logging.getLogger('test.ryuu.unsafe'),
    )
    assert asyncio.run(unsafe.fetch_ryuu_lua('480')) is None

    blocked = StubGet(
        lambda _url, _kwargs: (_ for _ in ()).throw(
            AssertionError('untrusted endpoint must not be requested')
        )
    )
    untrusted = RyuuSource(
        config={
            'Ryuu_Auth_Key': 'secret',
            'Ryuu_API_Base': 'https://attacker.test',
        },
        http_get=blocked,
        logger=logging.getLogger('test.ryuu.blocked'),
    )
    assert asyncio.run(untrusted.fetch_ryuu_lua('480')) is None
    assert blocked.calls == []


def test_ryuu_source_enforces_response_limit() -> None:
    get = StubGet(
        lambda url, _kwargs: _bytes_response(
            url,
            b'addappid(480)\n',
            headers={'Content-Length': str(MAX_LUA_BYTES + 1)},
        )
    )
    source = RyuuSource(
        config={'Ryuu_Auth_Key': 'secret'},
        http_get=get,
        logger=logging.getLogger('test.ryuu.limit'),
    )

    assert asyncio.run(source.fetch_ryuu_lua('480')) is None


def test_sudama_build_combines_validated_manifests_and_keys(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.sudama_store.write(
        {
            'keys': {'100': VALID_KEY},
            'ts': 1_000.0,
        }
    )
    depots = DepotStub({'100': '000123', '200': '456'})

    def no_network(_url: str, _kwargs: dict[str, Any]) -> httpx.Response:
        raise AssertionError('fresh cache should satisfy Lua assembly')

    source = SudamaSource(
        runtime=runtime,
        config={'Sudama_Cache_Hours': 24},
        http_get=StubGet(no_network),
        logger=logging.getLogger('test.sudama.build'),
        depot_source=depots,
        clock=lambda: 1_001.0,
    )

    lua = asyncio.run(source.build_lua_from_sudama('480'))

    assert lua == (
        'addappid(480)\n'
        f'addappid(100, 1, "{VALID_KEY.lower()}")\n'
        'setManifestid(100, "123")\n'
        'addappid(200)\n'
        'setManifestid(200, "456")\n'
    )
    assert depots.calls == ['480']


def test_composition_service_preserves_legacy_async_method_shapes(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    get = StubGet(lambda url, _kwargs: _json_response(url, {}, status=503))
    service = LibrarySourceService(
        runtime=runtime,
        config={},
        http_get=get,
        logger=logging.getLogger('test.library-sources'),
    )

    assert LibrarySources is LibrarySourceService
    assert tuple(inspect.signature(service.get_depot_manifest_map).parameters) == (
        'appid',
    )
    assert tuple(inspect.signature(service.get_sudama_keys).parameters) == ()
    assert tuple(inspect.signature(service.fetch_ryuu_lua).parameters) == ('appid',)
    assert tuple(inspect.signature(service.build_lua_from_sudama).parameters) == (
        'appid',
    )
    assert asyncio.run(service.get_depot_manifest_map('480')) == {}
