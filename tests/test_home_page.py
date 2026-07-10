from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import app.pages.home as home
from app.pages.home import HomePage


class FakeBackend:
    steam_path: Path | None = None

    def initialize(self) -> tuple[bool, str]:
        return True, ''

    def list_installed(self) -> list[dict[str, str]]:
        return [{'appid': '480'}]

    async def __aenter__(self) -> 'FakeBackend':
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def fetch_recommendations(self) -> list[dict[str, str]]:
        return []

    async def search_games(self, _query: str) -> list[dict[str, str]]:
        return []


class NoScheduledCallbacks:
    @staticmethod
    def singleShot(*_args: object) -> None:  # noqa: N802 - Qt API compatibility
        return None


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch) -> type[HomePage]:
    config = {
        'Custom_Steam_Path': '',
        'Ryuu_Auth_Key': '',
        'default_source': 'sudama',
    }
    monkeypatch.setattr(home, 'QTimer', NoScheduledCallbacks)
    monkeypatch.setattr(
        home,
        'load_config',
        lambda *, runtime=None: dict(config),
    )
    monkeypatch.setattr(home, 'detect_steam_path', lambda _custom='': None)
    monkeypatch.setattr(
        home,
        'ost_installed',
        lambda _path, *, runtime=None: False,
    )
    return HomePage


def test_home_page_constructs_offscreen_with_existing_contract(
    qtbot: Any,
    isolated_home: type[HomePage],
) -> None:
    page = isolated_home(lambda: FakeBackend())
    qtbot.addWidget(page)

    assert page.objectName() == 'home'
    assert isinstance(page.backend_provider(), FakeBackend)
    assert page.source_combo.currentIndex() == 2
    assert page._installed_appids == {'480'}
    assert page.cards == []
    assert page._content_generation == 0


def test_empty_search_restarts_recommendations_with_current_generation(
    qtbot: Any,
    isolated_home: type[HomePage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[object] = []
    monkeypatch.setattr(
        home,
        'start_registered_worker',
        lambda _owner, worker, _resources=None: started.append(worker) or True,
    )
    page = isolated_home(lambda: FakeBackend())
    qtbot.addWidget(page)
    page._loaded_recommendations = True

    page.do_search('   ')

    assert page._loaded_recommendations is True
    assert page._content_generation == 2
    assert page._rec_worker is started[0]
    assert page._rec_worker.property('generation') == 2
    assert page.hint_label.text() == '正在載入推薦遊戲…'


def test_search_cancels_older_workers_and_stale_results_are_ignored(
    qtbot: Any,
    isolated_home: type[HomePage],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RunningWorker:
        def __init__(self) -> None:
            self.cancelled = False

        def isRunning(self) -> bool:  # noqa: N802 - Qt API compatibility
            return True

        def request_cancel(self) -> None:
            self.cancelled = True

    started: list[object] = []
    monkeypatch.setattr(
        home,
        'start_registered_worker',
        lambda _owner, worker, _resources=None: started.append(worker) or True,
    )
    page = isolated_home(lambda: FakeBackend())
    qtbot.addWidget(page)
    old_search = RunningWorker()
    old_recommendations = RunningWorker()
    page._search_worker = old_search  # type: ignore[assignment]
    page._rec_worker = old_recommendations  # type: ignore[assignment]

    page.do_search('Spacewar')

    assert old_search.cancelled is True
    assert old_recommendations.cancelled is True
    assert page._search_worker is started[0]
    assert page._search_worker.property('generation') == 1
    page.hint_label.setText('current content')
    page._on_search_ok([{'appid': '10', 'name': 'stale'}], generation=0)
    page._on_recommendations_err(RuntimeError('stale'), generation=0)
    assert page.hint_label.text() == 'current content'
    assert page.cards == []
