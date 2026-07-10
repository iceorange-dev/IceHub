from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
from pathlib import Path
import shutil
import sys
import time
from types import ModuleType
import zipfile

import httpx
import pytest

from backend.runtime import BackendRuntime
from backend.storage import AppPaths


VALID_DEPOT_KEY = '0123456789abcdef' * 4
OST_FILES = ('OpenSteamTool.dll', 'dwmapi.dll', 'xinput1_4.dll')


def _pe(payload: bytes) -> bytes:
    """Return the smallest PE-shaped fixture accepted by the backend preflight."""

    return b'MZ' + (b'\0' * 62) + payload


def _write_ost_zip(path: Path, payload_prefix: bytes = b'new') -> Path:
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in OST_FILES:
            archive.writestr(f'release/bin/{name}', _pe(payload_prefix + name.encode()))
    return path


class BackendHarness:
    def __init__(self, module: ModuleType, runtime: BackendRuntime) -> None:
        self.module = module
        self.runtime = runtime


def _backend_instance(
    module: ModuleType,
    runtime: BackendRuntime,
    client: httpx.AsyncClient,
    **config,
):
    """Construct OSTBackend without loading a user's real configuration."""

    return module.OSTBackend(
        runtime=runtime,
        config={**module.DEFAULT_CONFIG, **config},
        client=client,
    )


def test_backend_constructor_reuses_injected_client_without_taking_ownership(
    isolated_backend,
) -> None:
    module = isolated_backend.module
    runtime = isolated_backend.runtime

    async def exercise() -> None:
        async with httpx.AsyncClient() as client:
            backend = module.OSTBackend(
                runtime=runtime,
                config={'default_source': 'sudama'},
                client=client,
            )

            async with backend as active:
                assert active is backend
                assert active.client is client

            assert client.is_closed is False
            assert backend.config == {'default_source': 'sudama'}
            assert backend.runtime is runtime

    asyncio.run(exercise())


@pytest.fixture
def isolated_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect every mutable backend root away from Steam and LocalAppData."""

    # This is set before the lazy import so even module-level path discovery cannot
    # observe the developer's real LocalAppData directory.
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local-app-data'))
    module = importlib.import_module('backend.ost_backend')
    paths = AppPaths.discover(
        development_root=tmp_path / 'icehub-data',
        resource_root=tmp_path / 'resources',
    )
    paths.ensure_writable_dirs()
    runtime = BackendRuntime.from_paths(
        paths,
        legacy_root=tmp_path / 'legacy',
        log=logging.getLogger('IceHub.tests'),
    )
    return BackendHarness(module, runtime)


def test_download_file_verifies_size_and_sha256_before_publish(
    isolated_backend, tmp_path: Path
) -> None:
    module = isolated_backend.module
    payload = b'authenticated release payload'
    progress: list[tuple[int, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == 'downloads.test'
        return httpx.Response(
            200,
            content=payload,
            headers={'Content-Length': str(len(payload))},
        )

    destination = tmp_path / 'downloads' / 'release.zip'
    async def exercise() -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await module.download_file(
                client,
                'https://downloads.test/release.zip',
                destination,
                lambda current, total: progress.append((current, total)),
                expected_size=len(payload),
                expected_digest=f'sha256:{hashlib.sha256(payload).hexdigest()}',
                max_bytes=len(payload),
                allowed_hosts={'downloads.test'},
            )

    result = asyncio.run(exercise())

    assert result is True
    assert destination.read_bytes() == payload
    assert progress[-1] == (len(payload), len(payload))
    assert not list(destination.parent.glob(f'.{destination.name}.*.part'))


@pytest.mark.parametrize('failure', ['digest', 'size-limit', 'host'])
def test_download_file_failure_never_publishes_partial_destination(
    isolated_backend, tmp_path: Path, failure: str
) -> None:
    module = isolated_backend.module
    payload = b'untrusted payload'
    requests_seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return httpx.Response(
            200,
            content=payload,
            headers={'Content-Length': str(len(payload))},
        )

    destination = tmp_path / failure / 'release.zip'
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b'previous verified release')
    digest = hashlib.sha256(payload).hexdigest()
    url = 'https://downloads.test/release.zip'
    max_bytes = len(payload)
    if failure == 'digest':
        digest = '0' * 64
    elif failure == 'size-limit':
        max_bytes = len(payload) - 1
    else:
        url = 'https://attacker.test/release.zip'

    async def exercise() -> bool:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await module.download_file(
                client,
                url,
                destination,
                expected_size=len(payload),
                expected_digest=f'sha256:{digest}',
                max_bytes=max_bytes,
                allowed_hosts={'downloads.test'},
            )

    result = asyncio.run(exercise())

    assert result is False
    assert destination.read_bytes() == b'previous verified release'
    assert not list(destination.parent.glob(f'.{destination.name}.*.part'))
    if failure in {'size-limit', 'host'}:
        assert requests_seen == []
    else:
        assert len(requests_seen) == 1


@pytest.mark.parametrize(
    ('response_lua', 'accepted'),
    [
        ('addappid(480)\nos.execute("calc")\n', False),
        (
            '-- generated data\n'
            'addappid(480)\n'
            f'addappid(100, 1, "{VALID_DEPOT_KEY}")\n'
            'setManifestid(100, "123456")\n',
            True,
        ),
    ],
)
def test_ryuu_response_must_match_the_data_only_lua_grammar(
    isolated_backend, response_lua: str, accepted: bool
) -> None:
    module = isolated_backend.module
    runtime = isolated_backend.runtime
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == 'generator.ryuu.lol'
        return httpx.Response(200, content=response_lua.encode('utf-8'))

    async def exercise() -> str | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = _backend_instance(
                module,
                runtime,
                client,
                Ryuu_Auth_Key='test-only-key',
            )
            return await backend.fetch_ryuu_lua('480')

    result = asyncio.run(exercise())

    assert result == (response_lua if accepted else None)


def test_sudama_filters_key_and_gid_injection_before_building_lua(
    isolated_backend,
) -> None:
    module = isolated_backend.module
    runtime = isolated_backend.runtime
    malicious_key = VALID_DEPOT_KEY[:-1] + '"); os.execute("calc") --'
    malicious_gid = '123"); os.execute("calc") --'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == 'api.993499094.xyz':
            return httpx.Response(
                200,
                json={
                    '100': VALID_DEPOT_KEY.upper(),
                    '101': malicious_key,
                    '102); os.execute("calc") --': VALID_DEPOT_KEY,
                },
            )
        assert request.url.host == 'api.steamcmd.net'
        return httpx.Response(
            200,
            json={
                'data': {
                    '480': {
                        'depots': {
                            '100': {'manifests': {'public': {'gid': '000123'}}},
                            '101': {'manifests': {'public': {'gid': malicious_gid}}},
                            '102); os.execute("calc") --': {
                                'manifests': {'public': {'gid': '456'}}
                            },
                        }
                    }
                }
            },
        )

    async def exercise() -> tuple[dict[str, str], dict[str, str], str | None]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            backend = _backend_instance(module, runtime, client)
            keys = await backend.get_sudama_keys()
            manifests = await backend.get_depot_manifest_map('480')
            lua = await backend.build_lua_from_sudama('480')
            return keys, manifests, lua

    keys, manifests, lua = asyncio.run(exercise())

    assert keys == {'100': VALID_DEPOT_KEY}
    assert manifests == {'100': '123'}
    assert lua == (
        'addappid(480)\n'
        f'addappid(100, 1, "{VALID_DEPOT_KEY}")\n'
        'setManifestid(100, "123")\n'
    )
    assert lua is not None
    assert 'os.execute' not in lua


def test_install_ost_rolls_back_every_target_when_commit_fails(
    isolated_backend, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = isolated_backend.module
    runtime = isolated_backend.runtime
    deployment_module = importlib.import_module('backend.deployment')
    steam = tmp_path / 'fake-steam'
    steam.mkdir()
    originals = {name: _pe(b'original-' + name.encode()) for name in OST_FILES}
    for name, content in originals.items():
        (steam / name).write_bytes(content)
    archive = _write_ost_zip(tmp_path / 'ost.zip')

    real_replace_from = deployment_module._replace_from
    calls = 0

    def fail_during_second_target(source: Path, target: Path, *, transaction_id: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('injected deployment failure')
        real_replace_from(source, target, transaction_id=transaction_id)

    monkeypatch.setattr(deployment_module, '_replace_from', fail_during_second_target)
    success, _message, deployed = module.install_ost_from_zip(
        archive, steam, runtime=runtime
    )

    assert success is False
    assert deployed == []
    assert {name: (steam / name).read_bytes() for name in OST_FILES} == originals
    assert not list(steam.glob('.icehub-transaction-*'))
    assert not list((runtime.state_dir / 'manifests').glob('ost-*.json'))
    assert not list(runtime.temp_dir.glob('ost-stage-*'))


def test_install_ost_migrates_legacy_backup_and_uninstall_restores_it(
    isolated_backend, tmp_path: Path
) -> None:
    module = isolated_backend.module
    runtime = isolated_backend.runtime
    steam = tmp_path / 'fake-steam'
    legacy_backup = steam / '.ost_backup'
    legacy_backup.mkdir(parents=True)
    originals = {name: _pe(b'original-' + name.encode()) for name in OST_FILES}
    for name in OST_FILES:
        (steam / name).write_bytes(_pe(b'legacy-ost-' + name.encode()))
        (legacy_backup / name).write_bytes(originals[name])
    archive = _write_ost_zip(tmp_path / 'ost.zip', payload_prefix=b'current-ost-')

    installed, _message, deployed = module.install_ost_from_zip(
        archive, steam, version='v-test', runtime=runtime
    )
    assert installed is True
    assert {Path(path).name for path in deployed} == set(OST_FILES)
    assert all((steam / name).read_bytes() != originals[name] for name in OST_FILES)

    removed, _message, changed = module.uninstall_ost(steam, runtime=runtime)
    assert removed is True
    assert set(OST_FILES).issubset(changed)
    assert {name: (steam / name).read_bytes() for name in OST_FILES} == originals
    # Pre-manifest backup directories can contain unrelated user files and
    # have no trustworthy ownership record, so uninstall preserves them.
    assert legacy_backup.is_dir()
    assert {name: (legacy_backup / name).read_bytes() for name in OST_FILES} == originals


def test_steamless_install_rejects_zip_slip_without_replacing_existing_tool(
    isolated_backend, tmp_path: Path
) -> None:
    module = isolated_backend.module
    runtime = isolated_backend.runtime
    steamless_dir = runtime.steamless_dir
    steamless_dir.mkdir(parents=True)
    (steamless_dir / 'keep.txt').write_bytes(b'existing tool')
    archive_path = tmp_path / 'steamless-malicious.zip'
    with zipfile.ZipFile(archive_path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('package/Steamless.CLI.exe', _pe(b'cli'))
        archive.writestr('package/Plugins/plugin.dll', _pe(b'plugin'))
        archive.writestr('../escape.exe', b'owned')

    success, _message, deployed = module.install_steamless_from_zip(
        archive_path, runtime=runtime
    )

    assert success is False
    assert deployed == []
    assert (steamless_dir / 'keep.txt').read_bytes() == b'existing tool'
    assert not list(tmp_path.rglob('escape.exe'))
    assert not list(steamless_dir.parent.glob('.steamless-extract-*'))
    assert not list(steamless_dir.parent.glob('.steamless-stage-*'))


@pytest.mark.parametrize(
    ('cancel_immediately', 'timeout_seconds', 'expected_code', 'message'),
    [
        (True, 5.0, -2, '[cancelled]'),
        (False, 0.2, -3, '[timeout]'),
    ],
)
def test_silent_steamless_process_can_be_cancelled_or_timed_out(
    isolated_backend,
    tmp_path: Path,
    cancel_immediately: bool,
    timeout_seconds: float,
    expected_code: int,
    message: str,
) -> None:
    module = isolated_backend.module
    runtime = isolated_backend.runtime
    # Python accepts a script regardless of its .exe suffix.  Using the running
    # interpreter keeps this process test portable while still exercising the
    # production Popen/reader/termination path with a completely silent child.
    script = tmp_path / 'silent-target.exe'
    script.write_text('import time\ntime.sleep(30)\n', encoding='utf-8')
    runtime.steamless_dir.mkdir(parents=True, exist_ok=True)
    base_executable = Path(sys.base_prefix) / Path(sys.executable).name
    shutil.copy2(
        base_executable if base_executable.is_file() else Path(sys.executable),
        runtime.steamless_cli,
    )
    for runtime_dll in Path(sys.base_prefix).glob('python*.dll'):
        shutil.copy2(runtime_dll, runtime.steamless_dir / runtime_dll.name)
    managed_files = {
        str(path.relative_to(runtime.steamless_dir)).replace('\\', '/'):
            module.sha256_file(path)
        for path in runtime.steamless_dir.iterdir()
        if path.is_file()
    }
    (runtime.steamless_dir / module.STEAMLESS_INTERNAL_MANIFEST).write_text(
        json.dumps({
            'schema': 1,
            'product': 'steamless',
            'version': 'test',
            'files': managed_files,
        }),
        encoding='utf-8',
    )
    output: list[str] = []

    started = time.monotonic()
    success, exit_code, unpacked = module.unpack_with_steamless(
        script,
        {},
        output_cb=output.append,
        cancel_check=(lambda: cancel_immediately),
        timeout_seconds=timeout_seconds,
        runtime=runtime,
    )
    elapsed = time.monotonic() - started

    assert success is False
    assert exit_code == expected_code
    assert unpacked is None
    assert any(line.startswith(message) for line in output)
    assert elapsed < 5
