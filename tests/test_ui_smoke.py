"""Offscreen smoke tests for the extracted page and window modules."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest
from PyQt6.QtCore import QThread
from PyQt6.QtGui import QCloseEvent
from PyQt6.QtWidgets import QApplication

from app.services.context import AppServices


@dataclass(slots=True)
class UiHarness:
    facade: ModuleType
    shell: ModuleType
    pages: dict[str, ModuleType]
    services: AppServices
    fake_backend: type


@pytest.fixture
def isolated_ui(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: Any,
    tmp_path: Path,
) -> UiHarness:
    del qtbot
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local-app-data'))

    # qfluentwidgets may have observed a short-lived QCoreApplication in an
    # earlier test. Recreate its process-global config under pytest-qt's live
    # QApplication before importing any page widgets.
    from qfluentwidgets.common import config as fluent_config

    try:
        _ = fluent_config.qconfig.themeChanged
    except RuntimeError:
        stale_config = fluent_config.qconfig
        fresh_config = fluent_config.QConfig()
        for module_name, module in tuple(sys.modules.items()):
            if (
                module_name.startswith('qfluentwidgets')
                and getattr(module, 'qconfig', None) is stale_config
            ):
                monkeypatch.setattr(module, 'qconfig', fresh_config)

    page_modules = {
        name: importlib.import_module(f'app.pages.{name}')
        for name in (
            'home',
            'installed',
            'settings',
            'ost',
            'steamless',
            'krkrz',
        )
    }
    shell = importlib.import_module('app.shell.main_window')
    facade = importlib.import_module('app.ost_app')
    image_loader = importlib.import_module('app.services.image_loader')

    config = {
        'Custom_Steam_Path': '',
        'Ryuu_Auth_Key': '',
        'default_source': 'auto',
        'theme_mode': 'auto',
        'theme_color': '#ff8c00',
    }

    class FakeBackend:
        lua_dir = tmp_path / 'steam' / 'config' / 'lua'
        steam_path: Path | None = None

        def initialize(self) -> tuple[bool, str]:
            return True, ''

        def list_installed(self) -> list[dict[str, Any]]:
            return []

        async def __aenter__(self) -> 'FakeBackend':
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class NoScheduledCallbacks:
        @staticmethod
        def singleShot(*_args: object) -> None:  # noqa: N802 - Qt API
            return None

    timed_pages = ('home', 'installed', 'ost', 'steamless', 'krkrz')
    for name in timed_pages:
        monkeypatch.setattr(page_modules[name], 'QTimer', NoScheduledCallbacks)

    configured_pages = ('home', 'installed', 'settings', 'ost', 'steamless')
    for name in configured_pages:
        monkeypatch.setattr(
            page_modules[name],
            'load_config',
            lambda *, runtime=None, config=config: dict(config),
        )

    backend_pages = ('installed', 'ost', 'steamless')
    for name in backend_pages:
        monkeypatch.setattr(page_modules[name], 'OSTBackend', FakeBackend)

    monkeypatch.setattr(page_modules['home'], 'detect_steam_path', lambda *_args: None)
    monkeypatch.setattr(
        page_modules['home'],
        'ost_installed',
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        page_modules['installed'],
        'ost_installed',
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(page_modules['ost'], 'detect_steam_path', lambda *_args: None)
    monkeypatch.setattr(page_modules['ost'], 'is_steam_running', lambda: False)
    monkeypatch.setattr(
        page_modules['steamless'],
        'is_steamless_installed',
        lambda *, runtime=None: False,
    )
    monkeypatch.setattr(
        page_modules['krkrz'],
        'is_steamless_installed',
        lambda *, runtime=None: False,
    )
    monkeypatch.setattr(
        page_modules['krkrz'],
        'bundled_krkrz_dll',
        lambda *, runtime=None: None,
    )

    for module in page_modules.values():
        info_bar = getattr(module, 'InfoBar', None)
        if info_bar is None:
            continue
        for method in ('success', 'error', 'warning', 'info'):
            monkeypatch.setattr(
                info_bar,
                method,
                staticmethod(lambda **_kwargs: None),
            )

    monkeypatch.setattr(
        shell,
        'resource_path',
        lambda relative, *, runtime=None: tmp_path / 'resources' / relative,
    )
    monkeypatch.setattr(image_loader.ImageLoader, '_instance', None)

    services = AppServices(backend_provider=FakeBackend)
    return UiHarness(
        facade=facade,
        shell=shell,
        pages=page_modules,
        services=services,
        fake_backend=FakeBackend,
    )


@pytest.mark.parametrize(
    ('module_name', 'class_name', 'object_name'),
    [
        ('home', 'HomePage', 'home'),
        ('installed', 'InstalledPage', 'installed'),
        ('settings', 'SettingsPage', 'settings'),
        ('ost', 'OSTPage', 'ost'),
        ('steamless', 'SteamlessPage', 'steamless'),
        ('krkrz', 'KrkrzPage', 'krkrz'),
    ],
)
def test_each_extracted_page_constructs_offscreen(
    qtbot: Any,
    isolated_ui: UiHarness,
    module_name: str,
    class_name: str,
    object_name: str,
) -> None:
    page_class = getattr(isolated_ui.pages[module_name], class_name)
    page = (
        page_class(isolated_ui.services.backend_provider)
        if module_name == 'home'
        else page_class()
    )
    qtbot.addWidget(page)

    assert page.objectName() == object_name


def test_settings_save_emits_signal_and_updates_only_edited_fields(
    qtbot: Any,
    isolated_ui: UiHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = isolated_ui.pages['settings']
    updates: list[dict[str, str]] = []
    monkeypatch.setattr(
        settings,
        'update_config_fields',
        lambda fields, *, runtime=None: updates.append(fields),
    )
    page = settings.SettingsPage()
    qtbot.addWidget(page)
    page.steam_path_edit.setText(' C:/Steam ')
    page.ryuu_key_edit.setText(' secret ')
    page.source_combo.setCurrentIndex(2)

    with qtbot.waitSignal(page.saved, timeout=1000):
        page.save_btn.click()

    assert updates == [
        {
            'Custom_Steam_Path': 'C:/Steam',
            'Ryuu_Auth_Key': 'secret',
            'default_source': 'sudama',
        }
    ]


def test_extracted_main_window_uses_injected_services_and_closes_cleanly(
    qtbot: Any,
    isolated_ui: UiHarness,
) -> None:
    window = isolated_ui.shell.MainWindow(app_services=isolated_ui.services)
    qtbot.addWidget(window)

    assert window.app_services is isolated_ui.services
    assert window.home_page.backend_provider is isolated_ui.services.backend_provider
    assert isinstance(window.home_page.backend_provider(), isolated_ui.fake_backend)
    assert window.home_page.objectName() == 'home'
    assert window.settings_page.objectName() == 'settings'
    assert not isolated_ui.services.worker_registry.running_workers()

    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()


def test_page_worker_uses_captured_backend_provider_without_widget_lookup(
    qtbot: Any,
    isolated_ui: UiHarness,
) -> None:
    """Background backend work must not resolve services through QWidget."""

    ost = isolated_ui.pages['ost']
    window_calls_on_gui_thread: list[bool] = []

    class ThreadProbePage(ost.OSTPage):
        def window(self):
            app = QApplication.instance()
            assert app is not None
            window_calls_on_gui_thread.append(
                QThread.currentThread() is app.thread()
            )
            return super().window()

    class ProbeBackend:
        async def __aenter__(self) -> 'ProbeBackend':
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def fetch_ost_latest(self) -> None:
            return None

    page = ThreadProbePage(backend_provider=ProbeBackend)
    qtbot.addWidget(page)
    page.check_update()

    worker = page._check_worker
    assert worker is not None
    assert worker.wait(2_000)
    assert window_calls_on_gui_thread
    assert all(window_calls_on_gui_thread)


def test_static_page_and_shell_import_contract(isolated_ui: UiHarness) -> None:
    pages_package = importlib.import_module('app.pages')
    shell_package = importlib.import_module('app.shell')
    entrypoint = importlib.import_module('main')

    expected_pages = {
        'HomePage': 'home',
        'InstalledPage': 'installed',
        'SettingsPage': 'settings',
        'OSTPage': 'ost',
        'SteamlessPage': 'steamless',
        'KrkrzPage': 'krkrz',
    }
    for class_name, module_name in expected_pages.items():
        implementation = getattr(isolated_ui.pages[module_name], class_name)
        assert getattr(pages_package, class_name) is implementation
        assert getattr(isolated_ui.facade, class_name) is implementation

    assert shell_package.MainWindow is isolated_ui.shell.MainWindow
    assert isolated_ui.facade.MainWindow is isolated_ui.shell.MainWindow
    assert callable(entrypoint.main)
    assert callable(isolated_ui.facade.main)
    for class_name in expected_pages:
        assert isinstance(getattr(isolated_ui.facade, class_name), type)
