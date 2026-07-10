"""Focused offscreen tests for the extracted Steamless page."""

from __future__ import annotations

import importlib
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def steamless_page_module(
    monkeypatch: pytest.MonkeyPatch,
    qtbot: Any,
) -> ModuleType:
    del qtbot  # ensure QApplication exists before qfluentwidgets repair
    module = importlib.import_module('app.pages.steamless')

    from qfluentwidgets.common import config as fluent_config

    try:
        _ = fluent_config.qconfig.themeChanged
    except RuntimeError:
        stale_config = fluent_config.qconfig
        fresh_config = fluent_config.QConfig()
        for module_name, loaded in tuple(sys.modules.items()):
            if (
                module_name.startswith('qfluentwidgets')
                and getattr(loaded, 'qconfig', None) is stale_config
            ):
                monkeypatch.setattr(loaded, 'qconfig', fresh_config)

    class NoScheduledCallbacks:
        @staticmethod
        def singleShot(*_args: object) -> None:  # noqa: N802
            return None

    monkeypatch.setattr(module, 'QTimer', NoScheduledCallbacks)
    monkeypatch.setattr(
        module,
        'is_steamless_installed',
        lambda *, runtime=None: False,
    )
    monkeypatch.setattr(
        module,
        'load_config',
        lambda *, runtime=None: {'installed_steamless_version': ''},
    )
    monkeypatch.setattr(module.InfoBar, 'success', staticmethod(lambda **_kwargs: None))
    monkeypatch.setattr(module.InfoBar, 'warning', staticmethod(lambda **_kwargs: None))
    monkeypatch.setattr(module.InfoBar, 'error', staticmethod(lambda **_kwargs: None))
    return module


def test_steamless_page_constructs_with_expected_empty_state(
    qtbot: Any,
    steamless_page_module: ModuleType,
) -> None:
    from app.workers import SteamlessUnpackWorker

    page = steamless_page_module.SteamlessPage()
    qtbot.addWidget(page)

    assert page.objectName() == 'steamless'
    assert page.acceptDrops() is True
    assert page.file_list.count() == 0
    assert page._collect_files() == []
    assert page._unpack_worker is None
    assert page._last_output_path is None
    assert page.log_view.document().maximumBlockCount() == 2_000
    assert steamless_page_module.SteamlessUnpackWorker is SteamlessUnpackWorker


def test_file_validation_deduplicates_and_collects_only_existing_executables(
    qtbot: Any,
    steamless_page_module: ModuleType,
    tmp_path: Path,
) -> None:
    page = steamless_page_module.SteamlessPage()
    qtbot.addWidget(page)
    first = tmp_path / 'first.exe'
    second = tmp_path / 'SECOND.EXE'
    text = tmp_path / 'notes.txt'
    directory = tmp_path / 'folder.exe'
    first.write_bytes(b'first')
    second.write_bytes(b'second')
    text.write_text('not an executable', encoding='utf-8')
    directory.mkdir()

    assert page._add_file_if_valid(str(first)) is True
    assert page._add_file_if_valid(str(first)) is False
    assert page._add_file_if_valid(str(second)) is True
    assert page._add_file_if_valid(str(text)) is False
    assert page._add_file_if_valid(str(directory)) is False
    assert page._add_file_if_valid(str(tmp_path / 'missing.exe')) is False
    assert page._collect_files() == [first, second]


def test_collect_options_tracks_switches(
    qtbot: Any,
    steamless_page_module: ModuleType,
) -> None:
    page = steamless_page_module.SteamlessPage()
    qtbot.addWidget(page)
    expected = {
        key: default
        for key, _flag, default, _description in steamless_page_module.STEAMLESS_FLAGS
    }

    assert page._collect_options() == expected
    first_key = next(iter(page._opt_switches))
    page._opt_switches[first_key].setChecked(not expected[first_key])
    assert page._collect_options()[first_key] is not expected[first_key]


def test_run_guards_do_not_create_worker_without_tool_or_files(
    qtbot: Any,
    steamless_page_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = steamless_page_module.SteamlessPage()
    qtbot.addWidget(page)
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    monkeypatch.setattr(
        steamless_page_module.InfoBar,
        'error',
        staticmethod(lambda **kwargs: errors.append(kwargs)),
    )
    monkeypatch.setattr(
        steamless_page_module.InfoBar,
        'warning',
        staticmethod(lambda **kwargs: warnings.append(kwargs)),
    )

    page._on_run()
    assert page._unpack_worker is None
    assert len(errors) == 1

    monkeypatch.setattr(
        steamless_page_module,
        'is_steamless_installed',
        lambda *, runtime=None: True,
    )
    page._on_run()
    assert page._unpack_worker is None
    assert len(warnings) == 1


def test_running_and_cancel_guards_are_safe(
    qtbot: Any,
    steamless_page_module: ModuleType,
) -> None:
    page = steamless_page_module.SteamlessPage()
    qtbot.addWidget(page)

    class RunningWorker:
        def __init__(self) -> None:
            self.cancelled = False

        def isRunning(self) -> bool:  # noqa: N802 - Qt compatibility
            return True

        def cancel(self) -> None:
            self.cancelled = True

    update_worker = RunningWorker()
    page._check_worker = update_worker
    previous_latest_text = page.latest_ver_label.text()
    page.check_update()
    assert page.latest_ver_label.text() == previous_latest_text

    install_worker = RunningWorker()
    page._install_worker = install_worker
    previous_install_text = page.install_tool_btn.text()
    page.install_steamless()
    assert page.install_tool_btn.text() == previous_install_text

    unpack_worker = RunningWorker()
    page._unpack_worker = unpack_worker
    page._on_cancel()
    assert unpack_worker.cancelled is True
    assert page.cancel_btn.isEnabled() is False
    assert '[cancel]' in page.log_view.toPlainText()


def test_refresh_status_disables_run_when_tool_is_absent(
    qtbot: Any,
    steamless_page_module: ModuleType,
) -> None:
    page = steamless_page_module.SteamlessPage()
    qtbot.addWidget(page)

    page.refresh_status()

    assert page.run_btn.isEnabled() is False
    assert page.uninstall_tool_btn.isEnabled() is False
