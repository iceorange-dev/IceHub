"""Focused offscreen tests for the extracted OpenSteamTool page."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def ost_page_module(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: Any,
    tmp_path: Path,
) -> tuple[ModuleType, dict[str, str], Path, list[tuple[str, str]]]:
    del qtbot  # ensure QApplication exists before repairing qfluentwidgets
    module = importlib.import_module('app.pages.ost')

    from qfluentwidgets.common import config as fluent_config

    try:
        _ = fluent_config.qconfig.themeChanged
    except RuntimeError:
        stale_config = fluent_config.qconfig
        fresh_config = fluent_config.QConfig()
        for module_name, loaded_module in tuple(sys.modules.items()):
            if (
                module_name.startswith('qfluentwidgets')
                and getattr(loaded_module, 'qconfig', None) is stale_config
            ):
                monkeypatch.setattr(loaded_module, 'qconfig', fresh_config)

    steam = tmp_path / 'Steam'
    steam.mkdir()
    config = {
        'Custom_Steam_Path': str(steam),
        'installed_ost_version': 'v1.2.3',
    }
    dialogs: list[tuple[str, str]] = []

    class NoScheduledCallbacks:
        @staticmethod
        def singleShot(*_args: object) -> None:  # noqa: N802
            return None

    class FakeMessageBox:
        def __init__(self, title: str, content: str, _parent: object) -> None:
            dialogs.append((title, content))

        def exec(self) -> int:
            return 0

    monkeypatch.setattr(module, 'QTimer', NoScheduledCallbacks)
    monkeypatch.setattr(module, 'MessageBox', FakeMessageBox)
    monkeypatch.setattr(
        module,
        'load_config',
        lambda *, runtime=None: dict(config),
    )
    monkeypatch.setattr(module, 'detect_steam_path', lambda _custom='': steam)
    monkeypatch.setattr(
        module,
        'detect_ost',
        lambda _steam, *, runtime=None: {
            'OpenSteamTool.dll': True,
            'dwmapi.dll': True,
            'xinput1_4.dll': True,
        },
    )
    monkeypatch.setattr(module, 'is_steam_running', lambda: False)
    monkeypatch.setattr(module.InfoBar, 'error', staticmethod(lambda **_kwargs: None))
    monkeypatch.setattr(module.InfoBar, 'success', staticmethod(lambda **_kwargs: None))
    return module, config, steam, dialogs


def test_ost_page_constructs_and_renders_healthy_status(
    qtbot: Any,
    ost_page_module: tuple[ModuleType, dict[str, str], Path, list[tuple[str, str]]],
) -> None:
    module, _config, steam, _dialogs = ost_page_module
    page = module.OSTPage()
    qtbot.addWidget(page)

    page.refresh_status()

    assert page.objectName() == 'ost'
    assert page.keep_lua_check.isChecked() is True
    assert page._latest_release is None
    assert page._check_worker is None
    assert page._install_worker is None
    assert page._uninstall_worker is None
    assert page.local_ver_label.text() == '本地版本: v1.2.3'
    assert page.status_label.text() == (
        f'Steam 路徑: {steam}\n'
        '  ✓ OpenSteamTool.dll\n'
        '  ✓ dwmapi.dll\n'
        '  ✓ xinput1_4.dll\n\n'
        '✓ OST 已正確部署'
    )


def test_ost_page_status_reports_missing_component(
    qtbot: Any,
    ost_page_module: tuple[ModuleType, dict[str, str], Path, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _config, _steam, _dialogs = ost_page_module
    monkeypatch.setattr(
        module,
        'detect_ost',
        lambda _steam, *, runtime=None: {
            'OpenSteamTool.dll': True,
            'dwmapi.dll': False,
            'xinput1_4.dll': True,
        },
    )
    page = module.OSTPage()
    qtbot.addWidget(page)

    page.refresh_status()

    assert '  ✗ dwmapi.dll' in page.status_label.text()
    assert page.status_label.text().endswith(
        '請點選下方「安裝 / 更新」按鈕自動部署 OST。'
    )


@pytest.mark.parametrize(
    ('action', 'button_name', 'dialog_text'),
    [
        ('install_ost', 'install_btn', '安裝 OST 需要覆蓋 dll 檔案'),
        ('uninstall_ost', 'uninstall_btn', '解除安裝 OST 需要刪除 dll 檔案'),
    ],
)
def test_steam_running_guard_restores_button_without_starting_worker(
    qtbot: Any,
    ost_page_module: tuple[ModuleType, dict[str, str], Path, list[tuple[str, str]]],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    button_name: str,
    dialog_text: str,
) -> None:
    module, _config, _steam, dialogs = ost_page_module
    monkeypatch.setattr(module, 'is_steam_running', lambda: True)
    starts: list[object] = []
    monkeypatch.setattr(
        module,
        '_start_registered_worker',
        lambda *_args, **_kwargs: starts.append(object()) or True,
    )
    page = module.OSTPage()
    qtbot.addWidget(page)
    button = getattr(page, button_name)
    original_text = button.text()

    getattr(page, action)()

    assert button.isEnabled() is True
    assert button.text() == original_text
    assert starts == []
    assert dialogs[-1][0] == 'Steam 正在執行'
    assert dialog_text in dialogs[-1][1]


def test_pages_package_exports_ost_page(ost_page_module) -> None:
    module, _config, _steam, _dialogs = ost_page_module
    package = importlib.import_module('app.pages')

    assert package.OSTPage is module.OSTPage
