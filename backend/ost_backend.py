"""Backward-compatible import facade for the IceHub backend.

New code should import the focused modules directly.  The desktop application
and existing integrations can continue importing this module while the
implementation remains statically discoverable by Nuitka.
"""

from __future__ import annotations

from .cache import (
    current_sudama_store as _current_sudama_store,
    get_cached_header as get_cached_header,
    get_cached_name as get_cached_name,
    set_cached_header as set_cached_header,
    set_cached_name as set_cached_name,
)
from .config import (
    load_config as load_config,
    save_config as save_config,
    update_config_fields as update_config_fields,
)
from .constants import (
    DEFAULT_CONFIG as DEFAULT_CONFIG,
    GITHUB_API as GITHUB_API,
    GITHUB_DOWNLOAD_HOSTS as GITHUB_DOWNLOAD_HOSTS,
    KRKRZ_BUNDLED_REL as KRKRZ_BUNDLED_REL,
    KRKRZ_DLL_FILENAME as KRKRZ_DLL_FILENAME,
    KRKRZ_REPO as KRKRZ_REPO,
    MAX_LUA_BYTES as MAX_LUA_BYTES,
    MAX_RELEASE_DOWNLOAD_BYTES as MAX_RELEASE_DOWNLOAD_BYTES,
    MAX_SUDAMA_BYTES as MAX_SUDAMA_BYTES,
    OST_DEFAULT_REPO as OST_DEFAULT_REPO,
    OST_FILES as OST_FILES,
    STEAMLESS_DEFAULT_REPO as STEAMLESS_DEFAULT_REPO,
    STEAMLESS_FLAGS as STEAMLESS_FLAGS,
    STEAMLESS_INTERNAL_MANIFEST as STEAMLESS_INTERNAL_MANIFEST,
    TRUSTED_LEGACY_ASSET_DIGESTS as TRUSTED_LEGACY_ASSET_DIGESTS,
    TRUSTED_OST_FILE_HASHES as TRUSTED_OST_FILE_HASHES,
)
from .deployment import (
    DeploymentError as DeploymentError,
    ManagedFileDeployment as ManagedFileDeployment,
    OwnershipError as OwnershipError,
)
from .facade import OSTBackend as OSTBackend
from .http import (
    _read_response_limited as _read_response_limited,
    _validated_https_url as _validated_https_url,
    download_file as download_file,
)
from .installers.krkrz import (
    _KRKRZ_BACKUP_RE as _KRKRZ_BACKUP_RE,
    _select_legacy_krkrz_original as _select_legacy_krkrz_original,
    bundled_krkrz_dll as bundled_krkrz_dll,
    deploy_krkrz_patch as deploy_krkrz_patch,
    detect_krkrz_status as detect_krkrz_status,
    remove_krkrz_patch as remove_krkrz_patch,
)
from .installers.ost import (
    _prepare_legacy_ost_adoption as _prepare_legacy_ost_adoption,
    _remove_all_managed_lua as _remove_all_managed_lua,
    detect_ost as detect_ost,
    install_ost_from_zip as install_ost_from_zip,
    ost_installed as ost_installed,
    uninstall_ost as uninstall_ost,
)
from .installers.steamless import (
    STEAMLESS_CLI as STEAMLESS_CLI,
    STEAMLESS_DIR as STEAMLESS_DIR,
    STEAMLESS_MANIFEST as STEAMLESS_MANIFEST,
    _read_steamless_internal_manifest as _read_steamless_internal_manifest,
    _recover_steamless_removal as _recover_steamless_removal,
    _steamless_lock_path as _steamless_lock_path,
    _steamless_manifest_store as _steamless_manifest_store,
    _steamless_remove_journal_path as _steamless_remove_journal_path,
    _validate_steamless_ownership as _validate_steamless_ownership,
    install_steamless_from_zip as install_steamless_from_zip,
    is_steamless_installed as is_steamless_installed,
    pick_steamless_zip as pick_steamless_zip,
    uninstall_steamless as uninstall_steamless,
)
from .installers.steamless_runner import (
    _hidden_subprocess_kwargs as _hidden_subprocess_kwargs,
    _pick_backup_path as _pick_backup_path,
    replace_with_unpacked as replace_with_unpacked,
    unpack_with_steamless as unpack_with_steamless,
)
from .logging_setup import setup_logger as setup_logger
from .platform.files import (
    _is_link_or_reparse as _is_link_or_reparse,
    _validate_pe_file as _validate_pe_file,
)
from .platform.steam import (
    detect_steam_path as detect_steam_path,
    is_steam_running as is_steam_running,
)
from .releases import (
    _github_digest_hex as _github_digest_hex,
    _version_tuple as _version_tuple,
    fetch_latest_release as fetch_latest_release,
    pick_release_zip as pick_release_zip,
    version_newer as version_newer,
)
from .resources import resource_path as resource_path
from .runtime import (
    BackendRuntime as BackendRuntime,
    DEFAULT_RUNTIME as DEFAULT_RUNTIME,
    resolve_runtime as resolve_runtime,
)
from .security import (
    SecurityValidationError as SecurityValidationError,
    sha256_file as sha256_file,
    validate_depot_key as validate_depot_key,
    validate_manifest_gid as validate_manifest_gid,
    validate_remote_lua as validate_remote_lua,
)
from .storage import CorruptJsonError as CorruptJsonError


# Historical path and store aliases.  Mutable state itself is owned by
# ``BackendRuntime``; these names exist only for older callers.
PATHS = DEFAULT_RUNTIME.paths
RESOURCE_ROOT = DEFAULT_RUNTIME.resource_root
APP_ROOT = DEFAULT_RUNTIME.app_root
STATE_DIR = DEFAULT_RUNTIME.state_dir
LEGACY_ROOT = DEFAULT_RUNTIME.legacy_root

CONFIG_DIR = PATHS.config_dir
CONFIG_FILE = CONFIG_DIR / 'config.json'
CREDENTIAL_FILE = CONFIG_DIR / 'credentials.json'
SUDAMA_CACHE = PATHS.cache_dir / 'sudama_cache.json'
NAME_CACHE = PATHS.cache_dir / 'name_cache.json'
HEADER_CACHE = PATHS.cache_dir / 'header_cache.json'

_name_cache = DEFAULT_RUNTIME.name_cache
_header_cache = DEFAULT_RUNTIME.header_cache
_cache_lock = DEFAULT_RUNTIME.cache_lock
_name_store = DEFAULT_RUNTIME.name_store
_header_store = DEFAULT_RUNTIME.header_store
_sudama_store = DEFAULT_RUNTIME.sudama_store
_config_store = DEFAULT_RUNTIME.config_store
LOG = DEFAULT_RUNTIME.log


__all__ = [
    '_current_sudama_store',
    'APP_ROOT',
    'BackendRuntime',
    'DEFAULT_CONFIG',
    'DEFAULT_RUNTIME',
    'GITHUB_API',
    'GITHUB_DOWNLOAD_HOSTS',
    'KRKRZ_BUNDLED_REL',
    'KRKRZ_DLL_FILENAME',
    'KRKRZ_REPO',
    'MAX_LUA_BYTES',
    'MAX_RELEASE_DOWNLOAD_BYTES',
    'MAX_SUDAMA_BYTES',
    'OSTBackend',
    'OST_DEFAULT_REPO',
    'OST_FILES',
    'STEAMLESS_CLI',
    'STEAMLESS_DEFAULT_REPO',
    'STEAMLESS_DIR',
    'STEAMLESS_FLAGS',
    'STEAMLESS_INTERNAL_MANIFEST',
    'TRUSTED_LEGACY_ASSET_DIGESTS',
    'TRUSTED_OST_FILE_HASHES',
    'bundled_krkrz_dll',
    'deploy_krkrz_patch',
    'detect_krkrz_status',
    'detect_ost',
    'detect_steam_path',
    'download_file',
    'fetch_latest_release',
    'get_cached_header',
    'get_cached_name',
    'install_ost_from_zip',
    'install_steamless_from_zip',
    'is_steam_running',
    'is_steamless_installed',
    'load_config',
    'ost_installed',
    'pick_release_zip',
    'pick_steamless_zip',
    'remove_krkrz_patch',
    'replace_with_unpacked',
    'resource_path',
    'save_config',
    'set_cached_header',
    'set_cached_name',
    'setup_logger',
    'uninstall_ost',
    'uninstall_steamless',
    'unpack_with_steamless',
    'update_config_fields',
    'version_newer',
]
