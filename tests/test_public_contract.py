"""Characterization tests for IceHub's current public module boundary.

These tests intentionally describe the imports and call shapes consumed by the
desktop application.  They allow implementation modules to be split while the
old facade remains compatible with existing callers.
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

from PyQt6.QtWidgets import QWidget

import app.ost_app as desktop_app
import backend.ost_backend as backend_module
import main as application_entrypoint


BACKEND_EXPORTS_USED_BY_APP = (
    'APP_ROOT',
    'KRKRZ_REPO',
    'OSTBackend',
    'OST_DEFAULT_REPO',
    'STEAMLESS_CLI',
    'STEAMLESS_DEFAULT_REPO',
    'STEAMLESS_DIR',
    'STEAMLESS_FLAGS',
    'bundled_krkrz_dll',
    'deploy_krkrz_patch',
    'detect_krkrz_status',
    'detect_ost',
    'detect_steam_path',
    'get_cached_header',
    'get_cached_name',
    'is_steam_running',
    'is_steamless_installed',
    'load_config',
    'ost_installed',
    'remove_krkrz_patch',
    'replace_with_unpacked',
    'resource_path',
    'set_cached_header',
    'setup_logger',
    'unpack_with_steamless',
    'update_config_fields',
    'version_newer',
)


def _parameter_names(callable_object: object) -> tuple[str, ...]:
    return tuple(inspect.signature(callable_object).parameters)


def _assert_backwards_compatible_parameters(
    callable_object: object,
    legacy_parameters: tuple[str, ...],
) -> None:
    """Allow new optional keyword-only injection points without breaking callers."""

    parameters = tuple(inspect.signature(callable_object).parameters.values())
    assert tuple(parameter.name for parameter in parameters[:len(legacy_parameters)]) == legacy_parameters
    for parameter in parameters[len(legacy_parameters):]:
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is not inspect.Parameter.empty


def test_backend_facade_exports_every_name_consumed_by_desktop_app() -> None:
    missing = [name for name in BACKEND_EXPORTS_USED_BY_APP if not hasattr(backend_module, name)]

    assert missing == []
    assert isinstance(backend_module.APP_ROOT, Path)
    assert isinstance(backend_module.STEAMLESS_DIR, Path)
    assert isinstance(backend_module.STEAMLESS_CLI, Path)
    assert all(isinstance(flag, tuple) and len(flag) == 4 for flag in backend_module.STEAMLESS_FLAGS)
    assert backend_module.OST_DEFAULT_REPO
    assert backend_module.STEAMLESS_DEFAULT_REPO
    assert backend_module.KRKRZ_REPO


def test_backend_free_function_call_shapes_used_by_app_are_stable() -> None:
    expected_parameters = {
        'resource_path': ('rel',),
        'get_cached_name': ('appid',),
        'get_cached_header': ('appid',),
        'set_cached_header': ('appid', 'url'),
        'load_config': (),
        'update_config_fields': ('fields',),
        'setup_logger': ('debug',),
        'detect_steam_path': ('custom',),
        'detect_ost': ('steam_path',),
        'ost_installed': ('steam_path',),
        'version_newer': ('a', 'b'),
        'is_steam_running': (),
        'is_steamless_installed': (),
        'unpack_with_steamless': (
            'exe_path',
            'options',
            'output_cb',
            'cancel_check',
            'timeout_seconds',
        ),
        'replace_with_unpacked': ('original', 'unpacked'),
        'bundled_krkrz_dll': (),
        'detect_krkrz_status': ('target_dir',),
        'deploy_krkrz_patch': ('target_dir', 'backup_existing'),
        'remove_krkrz_patch': ('target_dir', 'restore_backup'),
    }

    for name, parameters in expected_parameters.items():
        _assert_backwards_compatible_parameters(getattr(backend_module, name), parameters)

    assert inspect.signature(backend_module.setup_logger).parameters['debug'].default is False
    assert inspect.signature(backend_module.detect_steam_path).parameters['custom'].default == ''


def test_ost_backend_remains_no_arg_constructible() -> None:
    config = {'default_source': 'auto'}

    _assert_backwards_compatible_parameters(backend_module.OSTBackend, ())
    instance = backend_module.OSTBackend(config=config)

    assert instance.config == config
    assert instance.steam_path is None
    assert instance.lua_dir is None


def test_ost_backend_methods_consumed_by_pages_keep_their_signatures() -> None:
    expected_parameters = {
        'initialize': ('self',),
        'search_games': ('self', 'query', 'lang'),
        'fetch_recommendations': ('self',),
        'fetch_names_for': ('self', 'appids'),
        'install_game': ('self', 'appid', 'source'),
        'list_installed': ('self',),
        'remove_installed': ('self', 'appid'),
        'fetch_ost_latest': ('self',),
        'install_or_update_ost': ('self', 'progress_cb'),
        'uninstall_ost': ('self', 'keep_installed_games'),
        'fetch_steamless_latest': ('self',),
        'install_or_update_steamless': ('self', 'progress_cb'),
        'uninstall_steamless': ('self',),
    }

    for name, parameters in expected_parameters.items():
        method = getattr(backend_module.OSTBackend, name)
        _assert_backwards_compatible_parameters(method, parameters)

    install_signature = inspect.signature(backend_module.OSTBackend.install_game)
    assert install_signature.parameters['source'].default == 'auto'
    uninstall_signature = inspect.signature(backend_module.OSTBackend.uninstall_ost)
    assert uninstall_signature.parameters['keep_installed_games'].default is True


def test_ost_backend_basic_return_shapes_do_not_require_external_services(tmp_path: Path) -> None:
    instance = backend_module.OSTBackend(
        config={},
        steam_path_detector=lambda _custom='': None,
    )

    initialized, message = instance.initialize()
    assert isinstance(initialized, bool)
    assert isinstance(message, str)

    instance.lua_dir = tmp_path / 'missing-lua-directory'
    assert instance.list_installed() == []

    install_result = asyncio.run(instance.install_game('not-an-app-id'))
    assert isinstance(install_result, tuple)
    assert len(install_result) == 2
    assert isinstance(install_result[0], bool)
    assert isinstance(install_result[1], str)

    assert asyncio.run(instance.search_games('   ')) == []
    assert asyncio.run(instance.fetch_names_for([])) == {}


def test_desktop_module_keeps_importable_application_and_page_classes() -> None:
    page_classes = (
        desktop_app.HomePage,
        desktop_app.InstalledPage,
        desktop_app.SettingsPage,
        desktop_app.OSTPage,
        desktop_app.SteamlessPage,
        desktop_app.KrkrzPage,
    )

    assert callable(desktop_app.main)
    _assert_backwards_compatible_parameters(desktop_app.main, ())
    _assert_backwards_compatible_parameters(desktop_app.MainWindow.__init__, ('self',))
    assert issubclass(desktop_app.MainWindow, QWidget)
    assert all(issubclass(page_class, QWidget) for page_class in page_classes)
    _assert_backwards_compatible_parameters(
        desktop_app.HomePage.__init__, ('self', 'backend_provider', 'parent')
    )
    for page_class in page_classes[1:]:
        _assert_backwards_compatible_parameters(page_class.__init__, ('self', 'parent'))


def test_executable_entrypoint_remains_a_no_arg_callable() -> None:
    assert callable(application_entrypoint.main)
    _assert_backwards_compatible_parameters(application_entrypoint.main, ())
