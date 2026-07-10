"""Backward-compatible public facade for the IceHub desktop application.

The implementation is organized under ``app.pages``, ``app.services``,
``app.widgets``, ``app.workers``, and ``app.shell``.  Existing launchers and
extensions may keep importing the historical ``app.ost_app`` module.
"""

from __future__ import annotations

from app.bootstrap import main as main
from app.pages import (
    HomePage as HomePage,
    InstalledPage as InstalledPage,
    KrkrzPage as KrkrzPage,
    OSTPage as OSTPage,
    SettingsPage as SettingsPage,
    SteamlessPage as SteamlessPage,
)
from app.pages.common import error_text as _error_text
from app.services import (
    AppServices as AppServices,
    ImageLoader as ImageLoader,
    start_registered_worker as _start_registered_worker,
)
from app.shell import MainWindow as MainWindow
from app.tasking import (
    AsyncWorker as AsyncWorker,
    OperationCoordinator as OperationCoordinator,
    WorkerFailure as WorkerFailure,
    WorkerRegistry as WorkerRegistry,
)
from app.version import VERSION as VERSION
from app.widgets import GameCard as GameCard
from app.workers import (
    KrkrzWorker as KrkrzWorker,
    SteamlessUnpackWorker as SteamlessUnpackWorker,
)
from backend.ost_backend import (
    APP_ROOT as APP_ROOT,
    KRKRZ_REPO as KRKRZ_REPO,
    OSTBackend as OSTBackend,
    OST_DEFAULT_REPO as OST_DEFAULT_REPO,
    STEAMLESS_CLI as STEAMLESS_CLI,
    STEAMLESS_DEFAULT_REPO as STEAMLESS_DEFAULT_REPO,
    STEAMLESS_DIR as STEAMLESS_DIR,
    STEAMLESS_FLAGS as STEAMLESS_FLAGS,
    bundled_krkrz_dll as bundled_krkrz_dll,
    deploy_krkrz_patch as deploy_krkrz_patch,
    detect_krkrz_status as detect_krkrz_status,
    detect_ost as detect_ost,
    detect_steam_path as detect_steam_path,
    get_cached_header as get_cached_header,
    get_cached_name as get_cached_name,
    is_steam_running as is_steam_running,
    is_steamless_installed as is_steamless_installed,
    load_config as load_config,
    ost_installed as ost_installed,
    remove_krkrz_patch as remove_krkrz_patch,
    replace_with_unpacked as replace_with_unpacked,
    resource_path as resource_path,
    set_cached_header as set_cached_header,
    setup_logger as setup_logger,
    unpack_with_steamless as unpack_with_steamless,
    update_config_fields as update_config_fields,
    version_newer as version_newer,
)


__all__ = [
    '_error_text',
    '_start_registered_worker',
    'AppServices',
    'AsyncWorker',
    'GameCard',
    'HomePage',
    'ImageLoader',
    'InstalledPage',
    'KrkrzPage',
    'KrkrzWorker',
    'MainWindow',
    'OSTBackend',
    'OSTPage',
    'OperationCoordinator',
    'SettingsPage',
    'SteamlessPage',
    'SteamlessUnpackWorker',
    'VERSION',
    'WorkerFailure',
    'WorkerRegistry',
    'main',
]
