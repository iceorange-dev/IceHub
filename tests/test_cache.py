from __future__ import annotations

import json
from pathlib import Path

import backend.cache as cache
import backend.ost_backend as facade
from backend.runtime import BackendRuntime
from backend.storage import AppPaths


def _runtime(tmp_path: Path, name: str = 'data') -> BackendRuntime:
    paths = AppPaths.discover(
        development_root=tmp_path / name,
        resource_root=tmp_path / 'resources',
    )
    return BackendRuntime.from_paths(paths, legacy_root=tmp_path / 'legacy')


def test_load_caches_populates_only_the_requested_runtime(tmp_path: Path) -> None:
    first = _runtime(tmp_path, 'first')
    second = _runtime(tmp_path, 'second')
    first.name_store.write({'480': 'Spacewar', '999': ''})
    first.header_store.write({480: 'https://cdn.test/480.jpg'})

    assert first.name_cache == {}
    cache.load_caches(runtime=first)

    assert first.name_cache == {'480': 'Spacewar'}
    assert first.header_cache == {'480': 'https://cdn.test/480.jpg'}
    assert second.name_cache == {}
    assert second.header_cache == {}


def test_cache_updates_are_atomic_and_runtime_scoped(tmp_path: Path) -> None:
    first = _runtime(tmp_path, 'first')
    second = _runtime(tmp_path, 'second')

    cache.set_cached_name('480', 'Spacewar', runtime=first)
    cache.set_cached_header('480', 'https://cdn.test/480.jpg', runtime=first)

    assert cache.get_cached_name('480', runtime=first) == 'Spacewar'
    assert cache.get_cached_header('480', runtime=first).endswith('/480.jpg')
    assert first.name_store.read() == {'480': 'Spacewar'}
    assert cache.get_cached_name('480', runtime=second) == ''


def test_legacy_metadata_and_sudama_caches_migrate_once(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    legacy_config = runtime.legacy_root / 'config'
    legacy_config.mkdir(parents=True)
    documents = {
        'name_cache.json': {'480': 'Spacewar'},
        'header_cache.json': {'480': 'https://cdn.test/480.jpg'},
        'sudama_cache.json': {'keys': {'100': 'a' * 64}, 'ts': 123},
    }
    for filename, document in documents.items():
        (legacy_config / filename).write_text(
            json.dumps(document),
            encoding='utf-8',
        )

    cache.migrate_legacy_caches(runtime=runtime)

    assert runtime.name_store.read() == documents['name_cache.json']
    assert runtime.header_store.read() == documents['header_cache.json']
    assert cache.current_sudama_store(runtime=runtime).read() == documents[
        'sudama_cache.json'
    ]


def test_legacy_facade_reexports_cache_api() -> None:
    assert facade.get_cached_name is cache.get_cached_name
    assert facade.get_cached_header is cache.get_cached_header
    assert facade.set_cached_name is cache.set_cached_name
    assert facade.set_cached_header is cache.set_cached_header
