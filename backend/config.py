"""Runtime-scoped configuration loading, migration, and persistence."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .cache import load_caches, migrate_legacy_caches
from .constants import DEFAULT_CONFIG, OST_DEFAULT_REPO, STEAMLESS_DEFAULT_REPO
from .runtime import BackendRuntime, resolve_runtime
from .storage import CorruptJsonError, CredentialStore, CredentialStoreUnavailable


def credential_store(
    *,
    runtime: BackendRuntime | None = None,
) -> CredentialStore:
    return resolve_runtime(runtime).credentials()


def migrate_legacy_config(*, runtime: BackendRuntime | None = None) -> None:
    """Copy legacy beside-the-executable state into the data directory once."""

    active_runtime = resolve_runtime(runtime)
    if active_runtime.config_file.exists():
        return

    candidates: list[Path] = []
    compiled_dir = getattr(globals().get('__compiled__'), 'containing_dir', None)
    if getattr(sys, 'frozen', False) or compiled_dir:
        executable_dir = (
            Path(compiled_dir).resolve()
            if compiled_dir
            else Path(sys.executable).resolve().parent
        )
        candidates.append(executable_dir / 'config' / 'config.json')
    candidates.extend(
        [
            active_runtime.legacy_root / 'config' / 'config.json',
            active_runtime.resource_root / 'config' / 'config.json',
        ]
    )

    for legacy in candidates:
        try:
            if (
                legacy.resolve(strict=False)
                == active_runtime.config_file.resolve(strict=False)
            ):
                continue
            if not legacy.is_file():
                continue
            with legacy.open('r', encoding='utf-8') as stream:
                document = json.load(stream)
            if not isinstance(document, dict):
                continue

            migrated = dict(document)
            legacy_key = str(migrated.pop('Ryuu_Auth_Key', '') or '').strip()
            if legacy_key:
                try:
                    credential_store(runtime=active_runtime).set_ryuu_auth_key(
                        legacy_key
                    )
                except CredentialStoreUnavailable as error:
                    raise RuntimeError(
                        '無法啟用 Windows 安全憑證儲存，'
                        '已停止遷移含 Ryuu key 的舊設定'
                    ) from error
            active_runtime.config_store.write_if_missing(
                config_for_disk(migrated)
            )
            return
        except (OSError, json.JSONDecodeError):
            continue


def normalize_config(document: object) -> dict:
    if not isinstance(document, dict):
        raise ValueError('config.json 必須包含 JSON object')
    merged = DEFAULT_CONFIG.copy()
    merged.update(document)
    merged['config_version'] = 2
    merged['Custom_Steam_Path'] = str(
        merged.get('Custom_Steam_Path', '') or ''
    )
    merged['default_source'] = (
        merged.get('default_source')
        if merged.get('default_source') in {'auto', 'ryuu', 'sudama'}
        else 'auto'
    )
    merged['auto_fallback'] = bool(merged.get('auto_fallback', True))
    merged['debug_mode'] = bool(merged.get('debug_mode', False))
    merged['theme_mode'] = (
        merged.get('theme_mode')
        if merged.get('theme_mode') in {'auto', 'light', 'dark'}
        else 'auto'
    )
    color = str(merged.get('theme_color', DEFAULT_CONFIG['theme_color']))
    merged['theme_color'] = (
        color
        if re.fullmatch(r'#[0-9A-Fa-f]{6}', color)
        else DEFAULT_CONFIG['theme_color']
    )
    hours = merged.get('Sudama_Cache_Hours', 24)
    if not isinstance(hours, (int, float)) or isinstance(hours, bool):
        hours = 24
    merged['Sudama_Cache_Hours'] = max(0, min(float(hours), 24 * 30))

    # Executable and code sources are trusted release constants, never mutable
    # configuration supplied by a local JSON document.
    merged['Ryuu_API_Base'] = DEFAULT_CONFIG['Ryuu_API_Base']
    merged['Sudama_API'] = DEFAULT_CONFIG['Sudama_API']
    merged['OST_Repo'] = OST_DEFAULT_REPO
    merged['Steamless_Repo'] = STEAMLESS_DEFAULT_REPO
    return merged


def config_for_disk(config: dict) -> dict:
    document = normalize_config(config)
    document.pop('Ryuu_Auth_Key', None)
    return document


def load_config(*, runtime: BackendRuntime | None = None) -> dict:
    active_runtime = resolve_runtime(runtime)
    active_runtime.ensure_writable_dirs()
    migrate_legacy_config(runtime=active_runtime)
    migrate_legacy_caches(runtime=active_runtime)
    load_caches(runtime=active_runtime)
    active_runtime.config_store.write_if_missing(
        config_for_disk(DEFAULT_CONFIG.copy())
    )
    try:
        merged = normalize_config(active_runtime.config_store.read())
    except CorruptJsonError as error:
        raise RuntimeError(
            f'設定檔已損壞，IceHub 不會自動覆寫它：{error.path}'
        ) from error

    legacy_key = str(merged.pop('Ryuu_Auth_Key', '') or '').strip()
    try:
        credentials = credential_store(runtime=active_runtime)
        if legacy_key:
            credentials.set_ryuu_auth_key(legacy_key)
            active_runtime.config_store.delete_fields('Ryuu_Auth_Key')
        merged['Ryuu_Auth_Key'] = credentials.get_ryuu_auth_key() or ''
    except CredentialStoreUnavailable:
        # Never write a legacy plaintext key back when secure storage is
        # unavailable; retain it only in this in-memory result.
        merged['Ryuu_Auth_Key'] = legacy_key
    return merged


def save_config(
    config: dict,
    *,
    runtime: BackendRuntime | None = None,
) -> None:
    active_runtime = resolve_runtime(runtime)
    key_was_provided = 'Ryuu_Auth_Key' in config
    normalized = normalize_config(config)
    key = str(normalized.pop('Ryuu_Auth_Key', '') or '').strip()
    if key_was_provided:
        credentials = credential_store(runtime=active_runtime)
        if key:
            credentials.set_ryuu_auth_key(key)
        else:
            credentials.delete_ryuu_auth_key()
    active_runtime.config_store.write(normalized)


def update_config_fields(
    fields: dict,
    *,
    runtime: BackendRuntime | None = None,
) -> dict:
    """Patch only requested fields using the latest on-disk snapshot."""

    active_runtime = resolve_runtime(runtime)
    patch = dict(fields)
    if 'Ryuu_Auth_Key' in patch:
        key = str(patch.pop('Ryuu_Auth_Key') or '').strip()
        credentials = credential_store(runtime=active_runtime)
        if key:
            credentials.set_ryuu_auth_key(key)
        else:
            credentials.delete_ryuu_auth_key()
    if patch:

        def apply(document: object) -> dict:
            current = normalize_config(document)
            current.update(patch)
            return config_for_disk(normalize_config(current))

        active_runtime.config_store.update(apply)
    return load_config(runtime=active_runtime)


__all__ = [
    'config_for_disk',
    'credential_store',
    'load_config',
    'migrate_legacy_config',
    'normalize_config',
    'save_config',
    'update_config_fields',
]
