"""Focused offscreen tests for the extracted KRKRZ page."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def krkrz_page_module(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: Any,
    tmp_path: Path,
) -> tuple[ModuleType, Path, list[tuple[str, str]]]:
    del qtbot
    module = importlib.import_module('app.pages.krkrz')

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

    bundled = tmp_path / 'resources' / 'Version.dll'
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b'MZ' + b'\0' * 126)
    notices: list[tuple[str, str]] = []

    class NoScheduledCallbacks:
        @staticmethod
        def singleShot(*_args: object) -> None:  # noqa: N802
            return None

    def notice(**kwargs):
        notices.append((str(kwargs.get('title', '')), str(kwargs.get('content', ''))))

    monkeypatch.setattr(module, 'QTimer', NoScheduledCallbacks)
    monkeypatch.setattr(
        module,
        'bundled_krkrz_dll',
        lambda *, runtime=None: bundled,
    )
    monkeypatch.setattr(
        module,
        'detect_krkrz_status',
        lambda _directory, *, runtime=None: {
            'looks_like_krkrz': True,
            'deployed': False,
            'has_backup': False,
        },
    )
    monkeypatch.setattr(
        module,
        'is_steamless_installed',
        lambda *, runtime=None: False,
    )
    monkeypatch.setattr(module.InfoBar, 'info', staticmethod(notice))
    monkeypatch.setattr(module.InfoBar, 'warning', staticmethod(notice))
    monkeypatch.setattr(module.InfoBar, 'error', staticmethod(notice))
    monkeypatch.setattr(module.InfoBar, 'success', staticmethod(notice))
    return module, bundled, notices


def test_krkrz_page_constructor_defaults(
    qtbot: Any,
    krkrz_page_module: tuple[ModuleType, Path, list[tuple[str, str]]],
) -> None:
    module, bundled, _notices = krkrz_page_module
    page = module.KrkrzPage()
    qtbot.addWidget(page)

    page._refresh_resource_status()

    assert page.objectName() == 'krkrz'
    assert page.acceptDrops() is True
    assert page.target_list.count() == 0
    assert page.backup_switch.isChecked() is True
    assert page.restore_switch.isChecked() is True
    assert page.restore_switch.isEnabled() is False
    assert page.use_steamless_switch.isChecked() is False
    assert page.log_view.document().maximumBlockCount() == 2000
    assert page._worker is None
    assert page.deploy_btn.isEnabled() is True
    assert page.res_status_label.toolTip() == str(bundled)


def test_target_validation_deduplicates_and_collects_executables(
    qtbot: Any,
    krkrz_page_module: tuple[ModuleType, Path, list[tuple[str, str]]],
    tmp_path: Path,
) -> None:
    module, _bundled, _notices = krkrz_page_module
    page = module.KrkrzPage()
    qtbot.addWidget(page)
    executable = tmp_path / 'game' / 'GAME.EXE'
    executable.parent.mkdir()
    executable.write_bytes(b'MZ')
    invalid = tmp_path / 'readme.txt'
    invalid.write_text('not executable', encoding='utf-8')

    assert page._add_target_exe(executable) is True
    assert page._add_target_exe(executable) is False
    assert page._add_target_exe(invalid) is False
    assert page._add_target_exe(tmp_path / 'missing.exe') is False

    resolved = executable.resolve()
    assert page._collect_exes() == [resolved]
    assert 'KRKRZ ✓' in page.target_list.item(0).text()


def test_deploy_guard_rejects_missing_bundled_resource(
    qtbot: Any,
    krkrz_page_module: tuple[ModuleType, Path, list[tuple[str, str]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _bundled, notices = krkrz_page_module
    monkeypatch.setattr(
        module,
        'bundled_krkrz_dll',
        lambda *, runtime=None: None,
    )
    page = module.KrkrzPage()
    qtbot.addWidget(page)
    starts: list[object] = []
    monkeypatch.setattr(
        page,
        '_start_worker',
        lambda *_args: starts.append(object()),
    )

    page._on_deploy()

    assert starts == []
    assert notices[-1] == (
        '缺少 Version.dll',
        '內嵌資源遺失，請重新打包 IceHub',
    )


def test_deploy_guard_requires_steamless_when_option_is_enabled(
    qtbot: Any,
    krkrz_page_module: tuple[ModuleType, Path, list[tuple[str, str]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _bundled, notices = krkrz_page_module
    page = module.KrkrzPage()
    qtbot.addWidget(page)
    executable = tmp_path / 'game.exe'
    executable.write_bytes(b'MZ')
    assert page._add_target_exe(executable)
    page.use_steamless_switch.setChecked(True)
    starts: list[object] = []
    monkeypatch.setattr(
        page,
        '_start_worker',
        lambda *_args: starts.append(object()),
    )

    page._on_deploy()

    assert starts == []
    assert notices[-1][0] == 'Steamless 未安裝'
    assert 'Steamless 尚未安裝' in notices[-1][1]


def test_running_worker_guard_blocks_new_operation(
    qtbot: Any,
    krkrz_page_module: tuple[ModuleType, Path, list[tuple[str, str]]],
) -> None:
    module, _bundled, notices = krkrz_page_module
    page = module.KrkrzPage()
    qtbot.addWidget(page)

    class RunningWorker:
        @staticmethod
        def isRunning() -> bool:  # noqa: N802
            return True

    page._worker = RunningWorker()

    assert page._ensure_no_worker() is False
    assert notices[-1] == ('作業進行中', '請等待目前的部署/移除完成')


def test_pages_package_exports_krkrz_page(krkrz_page_module) -> None:
    module, _bundled, _notices = krkrz_page_module
    package = importlib.import_module('app.pages')

    assert package.KrkrzPage is module.KrkrzPage
