"""Focused offscreen tests for the extracted library and settings pages."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest
from PyQt6.QtGui import QFont, QPalette
from qfluentwidgets import Theme, setTheme


@pytest.fixture
def page_modules(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: Any,
    tmp_path: Path,
) -> tuple[ModuleType, ModuleType, dict[str, str]]:
    del qtbot  # ensures QApplication exists before qfluentwidgets is repaired
    installed = importlib.import_module('app.pages.installed')
    settings = importlib.import_module('app.pages.settings')

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

    config = {
        'Custom_Steam_Path': '',
        'Ryuu_Auth_Key': '',
        'default_source': 'auto',
    }

    class FakeBackend:
        lua_dir = tmp_path / 'Steam' / 'config' / 'lua'

        def __init__(self, *, runtime=None) -> None:
            self.runtime = runtime

        def initialize(self) -> tuple[bool, str]:
            return True, ''

        def list_installed(self) -> list[dict[str, object]]:
            return []

    class NoScheduledCallbacks:
        @staticmethod
        def singleShot(*_args: object) -> None:  # noqa: N802
            return None

    monkeypatch.setattr(installed, 'QTimer', NoScheduledCallbacks)
    monkeypatch.setattr(installed, 'OSTBackend', FakeBackend)
    monkeypatch.setattr(
        installed,
        'load_config',
        lambda *, runtime=None: dict(config),
    )
    monkeypatch.setattr(
        settings,
        'load_config',
        lambda *, runtime=None: dict(config),
    )
    monkeypatch.setattr(installed.InfoBar, 'error', staticmethod(lambda **_kwargs: None))
    monkeypatch.setattr(installed.InfoBar, 'success', staticmethod(lambda **_kwargs: None))
    monkeypatch.setattr(settings.InfoBar, 'error', staticmethod(lambda **_kwargs: None))
    monkeypatch.setattr(settings.InfoBar, 'success', staticmethod(lambda **_kwargs: None))
    return installed, settings, config


def test_extracted_pages_construct_offscreen(
    qtbot: Any,
    page_modules: tuple[ModuleType, ModuleType, dict[str, str]],
) -> None:
    installed, settings, _config = page_modules
    installed_page = installed.InstalledPage()
    settings_page = settings.SettingsPage()
    qtbot.addWidget(installed_page)
    qtbot.addWidget(settings_page)

    assert installed_page.objectName() == 'installed'
    assert settings_page.objectName() == 'settings'
    assert installed_page._rows == []
    assert installed_page._update_workers == {}
    assert installed_page._delete_workers == {}


def test_settings_save_emits_and_patches_only_edited_fields(
    qtbot: Any,
    page_modules: tuple[ModuleType, ModuleType, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _installed, settings, _config = page_modules
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

    with qtbot.waitSignal(page.saved, timeout=1_000):
        page.save_btn.click()

    assert updates == [
        {
            'Custom_Steam_Path': 'C:/Steam',
            'Ryuu_Auth_Key': 'secret',
            'default_source': 'sudama',
        }
    ]


def test_installed_page_empty_state_is_repeatable_and_safe(
    qtbot: Any,
    page_modules: tuple[ModuleType, ModuleType, dict[str, str]],
) -> None:
    installed, _settings, _config = page_modules
    page = installed.InstalledPage()
    qtbot.addWidget(page)

    page.refresh()
    page.refresh()

    assert page._installed_appids == []
    assert page._rows == []
    assert page.update_all_btn.isEnabled() is False
    assert str(Path('Steam') / 'config' / 'lua') in page.hint.text()


def _assert_dark_themed_bold_label(label: Any) -> None:
    style = label.styleSheet().strip().lower()
    color = label.palette().color(QPalette.ColorRole.WindowText)
    assert label.font().weight() == QFont.Weight.DemiBold
    assert 'font-weight' not in style
    assert 'color' in style
    assert color.lightness() > 127


def test_installed_and_settings_titles_keep_dark_theme_text_color(
    qtbot: Any,
    page_modules: tuple[ModuleType, ModuleType, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed, settings, _config = page_modules

    class NoopImageLoader:
        def load(self, *_args: object, **_kwargs: object) -> None:
            return None

    loader = NoopImageLoader()
    monkeypatch.setattr(
        installed.ImageLoader,
        'instance',
        classmethod(lambda _cls, **_kwargs: loader),
    )
    monkeypatch.setattr(
        installed,
        'get_cached_header',
        lambda _appid, *, runtime=None: '',
    )
    monkeypatch.setattr(
        installed,
        'get_cached_name',
        lambda _appid, *, runtime=None: 'Spacewar',
    )

    setTheme(Theme.DARK)
    try:
        installed_page = installed.InstalledPage()
        settings_page = settings.SettingsPage()
        qtbot.addWidget(installed_page)
        qtbot.addWidget(settings_page)
        installed_page._make_row('480', '480.lua')
        version_label = next(
            label
            for label in settings_page.findChildren(settings.BodyLabel)
            if label.text().startswith('IceHub v')
        )
        qtbot.wait(10)

        _assert_dark_themed_bold_label(installed_page._title_labels['480'])
        _assert_dark_themed_bold_label(version_label)
    finally:
        setTheme(Theme.AUTO)
