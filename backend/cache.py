"""Runtime-scoped metadata and Sudama cache helpers."""

from __future__ import annotations

import json

from .runtime import BackendRuntime, resolve_runtime
from .storage import AtomicJsonStore, CorruptJsonError


def current_sudama_store(
    *,
    runtime: BackendRuntime | None = None,
) -> AtomicJsonStore:
    """Return the Sudama cache store owned by ``runtime``."""

    return resolve_runtime(runtime).sudama_store


def load_caches(*, runtime: BackendRuntime | None = None) -> None:
    """Load name and header metadata into the runtime's in-memory caches."""

    active_runtime = resolve_runtime(runtime)
    try:
        data = active_runtime.name_store.read()
        names = (
            {str(key): str(value) for key, value in data.items() if value}
            if isinstance(data, dict)
            else {}
        )
    except (OSError, CorruptJsonError):
        names = {}
    with active_runtime.cache_lock:
        active_runtime.name_cache.clear()
        active_runtime.name_cache.update(names)

    try:
        data = active_runtime.header_store.read()
        headers = (
            {str(key): str(value) for key, value in data.items() if value}
            if isinstance(data, dict)
            else {}
        )
    except (OSError, CorruptJsonError):
        headers = {}
    with active_runtime.cache_lock:
        active_runtime.header_cache.clear()
        active_runtime.header_cache.update(headers)


def migrate_legacy_caches(*, runtime: BackendRuntime | None = None) -> None:
    """Copy legacy cache documents once without overwriting current state."""

    active_runtime = resolve_runtime(runtime)
    mappings = (
        ('name_cache.json', active_runtime.name_store),
        ('header_cache.json', active_runtime.header_store),
        ('sudama_cache.json', active_runtime.sudama_store),
    )
    for filename, store in mappings:
        if store.path.exists():
            continue
        for base in (active_runtime.legacy_root, active_runtime.resource_root):
            legacy = base / 'config' / filename
            try:
                if legacy.resolve(strict=False) == store.path.resolve(strict=False):
                    continue
                if legacy.is_file():
                    with legacy.open('r', encoding='utf-8') as stream:
                        document = json.load(stream)
                    store.write_if_missing(document)
                    break
            except (OSError, json.JSONDecodeError):
                continue


def get_cached_name(
    appid: str,
    *,
    runtime: BackendRuntime | None = None,
) -> str:
    active_runtime = resolve_runtime(runtime)
    with active_runtime.cache_lock:
        return active_runtime.name_cache.get(str(appid), '')


def set_cached_name(
    appid: str,
    name: str,
    *,
    runtime: BackendRuntime | None = None,
) -> None:
    active_runtime = resolve_runtime(runtime)
    if not name:
        return
    try:
        with active_runtime.cache_lock:
            updated = active_runtime.name_store.update_fields(
                {str(appid): str(name)}
            )
            active_runtime.name_cache.clear()
            active_runtime.name_cache.update(
                {str(key): str(value) for key, value in updated.items() if value}
            )
    except (OSError, CorruptJsonError) as error:
        active_runtime.log.warning(f'名稱快取寫入失敗: {error}')


def get_cached_header(
    appid: str,
    *,
    runtime: BackendRuntime | None = None,
) -> str:
    active_runtime = resolve_runtime(runtime)
    with active_runtime.cache_lock:
        return active_runtime.header_cache.get(str(appid), '')


def set_cached_header(
    appid: str,
    url: str,
    *,
    runtime: BackendRuntime | None = None,
) -> None:
    active_runtime = resolve_runtime(runtime)
    if not url:
        return
    try:
        with active_runtime.cache_lock:
            updated = active_runtime.header_store.update_fields(
                {str(appid): str(url)}
            )
            active_runtime.header_cache.clear()
            active_runtime.header_cache.update(
                {str(key): str(value) for key, value in updated.items() if value}
            )
    except (OSError, CorruptJsonError) as error:
        active_runtime.log.warning(f'圖片快取寫入失敗: {error}')


__all__ = [
    'current_sudama_store',
    'get_cached_header',
    'get_cached_name',
    'load_caches',
    'migrate_legacy_caches',
    'set_cached_header',
    'set_cached_name',
]
