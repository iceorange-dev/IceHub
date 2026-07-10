"""Focused composition and bootstrap tests without a real event loop or lock."""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
import sys
from types import ModuleType
from typing import Any

import pytest
from PyQt6.QtGui import QCloseEvent
from qfluentwidgets import Theme


def _repair_fluent_config(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_main_window_composes_six_pages_with_injected_services_and_closes(
    qtbot: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    shell = importlib.import_module('app.shell.main_window')
    context = importlib.import_module('app.services.context')
    pages = importlib.import_module('app.pages')
    _repair_fluent_config(monkeypatch)

    class NoScheduledCallbacks:
        @staticmethod
        def singleShot(*_args: object) -> None:  # noqa: N802
            return None

    for module_name in (
        'app.pages.home',
        'app.pages.installed',
        'app.pages.krkrz',
        'app.pages.ost',
        'app.pages.steamless',
    ):
        module = importlib.import_module(module_name)
        if hasattr(module, 'QTimer'):
            monkeypatch.setattr(module, 'QTimer', NoScheduledCallbacks)

    config = {
        'Custom_Steam_Path': '',
        'Ryuu_Auth_Key': '',
        'default_source': 'auto',
        'installed_ost_version': '',
        'installed_steamless_version': '',
    }
    for module_name in (
        'app.pages.home',
        'app.pages.installed',
        'app.pages.ost',
        'app.pages.settings',
        'app.pages.steamless',
    ):
        module = importlib.import_module(module_name)
        if hasattr(module, 'load_config'):
            monkeypatch.setattr(
                module,
                'load_config',
                lambda *, runtime=None: dict(config),
            )

    monkeypatch.setattr(
        shell,
        'resource_path',
        lambda _relative, *, runtime=None: tmp_path / 'missing.ico',
    )
    monkeypatch.setattr(shell.ImageLoader, '_instance', None)

    class FakeBackend:
        pass

    created: list[FakeBackend] = []

    def backend_provider() -> FakeBackend:
        backend = FakeBackend()
        created.append(backend)
        return backend

    services = context.AppServices(backend_provider=backend_provider)
    window = shell.MainWindow(app_services=services)
    qtbot.addWidget(window)

    assert window.app_services is services
    assert window.worker_registry is services.worker_registry
    assert window.operation_coordinator is services.operation_coordinator
    assert window.home_page.backend_provider is services.backend_provider
    assert isinstance(window.home_page, pages.HomePage)
    assert isinstance(window.installed_page, pages.InstalledPage)
    assert isinstance(window.krkrz_page, pages.KrkrzPage)
    assert isinstance(window.ost_page, pages.OSTPage)
    assert isinstance(window.settings_page, pages.SettingsPage)
    assert isinstance(window.steamless_page, pages.SteamlessPage)
    assert all(
        page.runtime is services.runtime
        for page in (
            window.home_page,
            window.installed_page,
            window.krkrz_page,
            window.ost_page,
            window.settings_page,
            window.steamless_page,
        )
    )
    assert [
        window.home_page.objectName(),
        window.installed_page.objectName(),
        window.krkrz_page.objectName(),
        window.ost_page.objectName(),
        window.settings_page.objectName(),
        window.steamless_page.objectName(),
    ] == ['home', 'installed', 'krkrz', 'ost', 'settings', 'steamless']
    assert backend_provider() is created[-1]
    assert services.worker_registry.running_workers() == ()

    event = QCloseEvent()
    window.closeEvent(event)
    assert event.isAccepted()


@pytest.mark.parametrize(
    ('mode', 'expected'),
    [
        ('light', Theme.LIGHT),
        ('dark', Theme.DARK),
        ('auto', Theme.AUTO),
        ('unexpected', Theme.AUTO),
    ],
)
def test_bootstrap_theme_mapping(
    mode: str,
    expected: Theme,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = importlib.import_module('app.bootstrap')
    themes: list[Theme] = []
    colors: list[str] = []
    monkeypatch.setattr(bootstrap, 'setTheme', themes.append)
    monkeypatch.setattr(bootstrap, 'setThemeColor', colors.append)

    bootstrap._apply_theme({'theme_mode': mode, 'theme_color': '#123456'})

    assert themes == [expected]
    assert colors == ['#123456']


class FakeApplication:
    instances: list['FakeApplication'] = []
    rounding_policies: list[object] = []
    exit_code = 17

    def __init__(self, arguments: list[str]) -> None:
        self.arguments = list(arguments)
        self.icon = None
        self.exec_called = False
        self.instances.append(self)

    @classmethod
    def setHighDpiScaleFactorRoundingPolicy(cls, policy: object) -> None:  # noqa: N802
        cls.rounding_policies.append(policy)

    def setWindowIcon(self, icon: object) -> None:  # noqa: N802
        self.icon = icon

    def exec(self) -> int:
        self.exec_called = True
        return self.exit_code


class FakeLock:
    instances: list['FakeLock'] = []
    allow_lock = True

    def __init__(self, path: str) -> None:
        self.path = path
        self.stale_time: int | None = None
        self.timeout: int | None = None
        self.unlocked = False
        self.instances.append(self)

    def setStaleLockTime(self, milliseconds: int) -> None:  # noqa: N802
        self.stale_time = milliseconds

    def tryLock(self, timeout: int) -> bool:  # noqa: N802
        self.timeout = timeout
        return self.allow_lock

    def unlock(self) -> None:
        self.unlocked = True


def _patch_bootstrap_runtime(
    bootstrap: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[list[bool], list[object], list[object]]:
    FakeApplication.instances.clear()
    FakeApplication.rounding_policies.clear()
    FakeLock.instances.clear()
    FakeLock.allow_lock = True
    logger_modes: list[bool] = []
    themes: list[object] = []
    colors: list[object] = []

    monkeypatch.setattr(bootstrap, 'QApplication', FakeApplication)
    monkeypatch.setattr(bootstrap, 'QLockFile', FakeLock)
    monkeypatch.setattr(bootstrap, 'APP_ROOT', tmp_path / 'app-data')
    monkeypatch.setattr(bootstrap, '_set_windows_app_id', lambda: None)
    monkeypatch.setattr(
        bootstrap,
        'setup_logger',
        lambda debug=False: logger_modes.append(bool(debug)),
    )
    monkeypatch.setattr(
        bootstrap,
        'load_config',
        lambda: {
            'debug_mode': True,
            'theme_mode': 'dark',
            'theme_color': '#abcdef',
        },
    )
    monkeypatch.setattr(
        bootstrap,
        'resource_path',
        lambda _relative: tmp_path / 'missing.ico',
    )
    monkeypatch.setattr(bootstrap, 'setTheme', themes.append)
    monkeypatch.setattr(bootstrap, 'setThemeColor', colors.append)
    return logger_modes, themes, colors


def test_bootstrap_main_is_public_and_runs_with_fake_app_and_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bootstrap = importlib.import_module('app.bootstrap')
    logger_modes, themes, colors = _patch_bootstrap_runtime(
        bootstrap,
        monkeypatch,
        tmp_path,
    )

    class FakeWindow:
        instances: list['FakeWindow'] = []

        def __init__(self) -> None:
            self.shown = False
            self.instances.append(self)

        def show(self) -> None:
            self.shown = True

    monkeypatch.setattr(bootstrap, 'MainWindow', FakeWindow)

    assert callable(bootstrap.main)
    assert tuple(inspect.signature(bootstrap.main).parameters) == ()
    assert bootstrap.main() == FakeApplication.exit_code

    application = FakeApplication.instances[-1]
    lock = FakeLock.instances[-1]
    assert application.exec_called is True
    assert FakeWindow.instances[-1].shown is True
    assert lock.path == str(tmp_path / 'app-data' / 'icehub-instance.lock')
    assert lock.stale_time == 30_000
    assert lock.timeout == 100
    assert lock.unlocked is True
    assert logger_modes == [False, True]
    assert themes == [Theme.DARK]
    assert colors == ['#abcdef']


def test_bootstrap_lock_failure_never_constructs_window_or_enters_exec(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bootstrap = importlib.import_module('app.bootstrap')
    _patch_bootstrap_runtime(bootstrap, monkeypatch, tmp_path)
    FakeLock.allow_lock = False
    messages: list[tuple[object, str, str]] = []
    monkeypatch.setattr(
        bootstrap.QMessageBox,
        'information',
        staticmethod(lambda parent, title, text: messages.append((parent, title, text))),
    )

    class ForbiddenWindow:
        def __init__(self) -> None:
            raise AssertionError('window must not be built when lock acquisition fails')

    monkeypatch.setattr(bootstrap, 'MainWindow', ForbiddenWindow)

    assert bootstrap.main() == 0
    assert FakeApplication.instances[-1].exec_called is False
    assert FakeLock.instances[-1].unlocked is False
    assert len(messages) == 1


def test_shell_and_version_public_contract() -> None:
    shell = importlib.import_module('app.shell')
    version = importlib.import_module('app.version')
    parameters = tuple(inspect.signature(shell.MainWindow.__init__).parameters.values())

    assert parameters[0].name == 'self'
    assert parameters[1].name == 'app_services'
    assert parameters[1].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters[1].default is None
    assert version.VERSION == '1.0.0'
