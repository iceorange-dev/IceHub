"""Home catalog page with search, recommendations, and Lua installation."""

from __future__ import annotations

from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    FlowLayout,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    ProgressRing,
    ScrollArea,
    SearchLineEdit,
    TitleLabel,
    TransparentToolButton,
)

from app.pages.common import error_text as _error_text
from app.services.image_loader import ImageLoader
from app.services.tasks import start_registered_worker
from app.tasking import AsyncWorker
from app.version import VERSION
from app.widgets import GameCard
from backend.cache import get_cached_header
from backend.ost_backend import (
    detect_steam_path,
    load_config,
    ost_installed,
)
from backend.runtime import BackendRuntime, resolve_runtime


class HomePage(ScrollArea):
    def __init__(
        self,
        backend_provider,
        parent=None,
        *,
        runtime: BackendRuntime | None = None,
    ):
        super().__init__(parent)
        self.backend_provider = backend_provider
        self.runtime = resolve_runtime(runtime)
        self.setObjectName('home')
        self.setWidgetResizable(True)
        self.setStyleSheet(
            'QScrollArea { background: transparent; border: none; }'
        )

        container = QWidget()
        container.setStyleSheet('background: transparent;')
        self.setWidget(container)

        self.main_layout = QVBoxLayout(container)
        self.main_layout.setContentsMargins(30, 24, 30, 24)
        self.main_layout.setSpacing(16)

        title = TitleLabel('IceHub', container)
        subtitle = CaptionLabel(
            f'Steam 入庫工具（搭配 OpenSteamTool）· v{VERSION}',
            container,
        )
        subtitle.setTextColor('#666', '#aaa')
        self.main_layout.addWidget(title)
        self.main_layout.addWidget(subtitle)

        self.status_card = CardWidget(container)
        self.status_card.setBorderRadius(8)
        self.status_card.setFixedHeight(60)
        status_layout = QHBoxLayout(self.status_card)
        status_layout.setContentsMargins(16, 8, 16, 8)
        self.status_label = BodyLabel(
            '正在檢測 Steam / OST...',
            self.status_card,
        )
        status_layout.addWidget(self.status_label)
        status_layout.addStretch(1)
        self.refresh_status_btn = TransparentToolButton(
            FluentIcon.SYNC,
            self.status_card,
        )
        self.refresh_status_btn.clicked.connect(self.refresh_status)
        status_layout.addWidget(self.refresh_status_btn)
        self.main_layout.addWidget(self.status_card)

        search_row = QHBoxLayout()
        self.search_input = SearchLineEdit(container)
        self.search_input.setPlaceholderText('輸入遊戲名或 AppID')
        self.search_input.searchSignal.connect(self.do_search)
        self.search_input.returnPressed.connect(self._on_return_pressed)
        search_row.addWidget(self.search_input, 1)

        self.source_combo = ComboBox(container)
        self.source_combo.addItems(
            ['自動 (Ryuu → Sudama)', '僅 Ryuu', '僅 Sudama']
        )
        self.source_combo.setFixedWidth(180)
        self.reload_settings()
        search_row.addWidget(self.source_combo)
        self.main_layout.addLayout(search_row)

        self.hint_label = CaptionLabel('正在載入推薦遊戲…', container)
        self.hint_label.setTextColor('#888', '#aaa')
        self.main_layout.addWidget(self.hint_label)

        self.progress_ring = ProgressRing(container)
        self.progress_ring.setFixedSize(28, 28)
        self.progress_ring.hide()
        self.main_layout.addWidget(
            self.progress_ring,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )

        self.grid_widget = QWidget(container)
        self.grid_widget.setStyleSheet('background: transparent;')
        self.flow = FlowLayout(self.grid_widget, needAni=False)
        self.flow.setContentsMargins(0, 0, 0, 0)
        self.flow.setHorizontalSpacing(12)
        self.flow.setVerticalSpacing(12)
        self.main_layout.addWidget(self.grid_widget)
        self.main_layout.addStretch(1)

        self.cards: List[GameCard] = []
        self._search_worker: Optional[AsyncWorker] = None
        self._rec_worker: Optional[AsyncWorker] = None
        self._install_workers: Dict[str, AsyncWorker] = {}
        self._content_generation = 0
        self._loaded_recommendations = False
        self._installed_appids: set = set()
        self._refresh_installed_set()

        QTimer.singleShot(100, self.refresh_status)
        QTimer.singleShot(150, self._load_recommendations)

    def reload_settings(self) -> None:
        """Apply the configured default without overriding later choices."""

        source = load_config(runtime=self.runtime).get('default_source', 'auto')
        self.source_combo.setCurrentIndex(
            {'auto': 0, 'ryuu': 1, 'sudama': 2}.get(source, 0)
        )

    def _refresh_installed_set(self) -> None:
        """Read the currently installed AppIDs from the Lua library."""

        try:
            backend = self.backend_provider()
            ok, _message = backend.initialize()
            if ok:
                self._installed_appids = {
                    str(item['appid']) for item in backend.list_installed()
                }
            else:
                self._installed_appids = set()
        except Exception:
            self._installed_appids = set()

    def _apply_installed_state(self) -> None:
        for card in self.cards:
            card.set_installed(card.appid in self._installed_appids)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API override
        super().showEvent(event)
        self._refresh_installed_set()
        self._apply_installed_state()
        self.refresh_status()

    def refresh_status(self) -> None:
        config = load_config(runtime=self.runtime)
        steam = detect_steam_path(config.get('Custom_Steam_Path', ''))
        if not steam:
            self.status_label.setText(
                '⚠ 未找到 Steam，請在「設定」中指定路徑'
            )
            return
        ost_status = ost_installed(steam, runtime=self.runtime)
        ryuu_key = config.get('Ryuu_Auth_Key', '').strip()
        messages = [f'Steam: {steam}']
        messages.append(f'OST: {"已安裝" if ost_status else "✗ 未安裝"}')
        messages.append(
            f'Ryuu Key: {"已設定" if ryuu_key else "✗ 未設定"}'
        )
        self.status_label.setText('   |   '.join(messages))

    def _load_recommendations(self) -> None:
        if self._loaded_recommendations:
            return
        self._loaded_recommendations = True
        self._content_generation += 1
        generation = self._content_generation
        self.hint_label.setText('正在載入推薦遊戲…')
        self.progress_ring.show()

        async def fetch():
            async with self.backend_provider() as backend:
                return await backend.fetch_recommendations()

        self._rec_worker = AsyncWorker(fetch)
        self._rec_worker.setProperty('generation', generation)
        self._rec_worker.result.connect(self._on_recommendations_result)
        self._rec_worker.error.connect(self._on_recommendations_worker_error)
        start_registered_worker(self, self._rec_worker)

    def _on_recommendations_result(self, games: List[Dict]) -> None:
        worker = self.sender()
        generation = (
            int(worker.property('generation'))
            if isinstance(worker, QObject)
            else -1
        )
        self._on_recommendations(games, generation)

    def _on_recommendations_worker_error(self, error: object) -> None:
        worker = self.sender()
        generation = (
            int(worker.property('generation'))
            if isinstance(worker, QObject)
            else -1
        )
        self._on_recommendations_err(error, generation)

    def _on_recommendations(
        self,
        games: List[Dict],
        generation: int,
    ) -> None:
        if generation != self._content_generation:
            return
        self.progress_ring.hide()
        if not games:
            self.hint_label.setText('暫無推薦，可直接搜尋')
            return
        self.hint_label.setText(f'熱門推薦 ({len(games)})')
        self._render_cards(games)

    def _on_recommendations_err(
        self,
        error: object,
        generation: int,
    ) -> None:
        if generation != self._content_generation:
            return
        self.progress_ring.hide()
        self.hint_label.setText(f'推薦載入失敗: {_error_text(error)}')

    def _on_return_pressed(self) -> None:
        self.do_search(self.search_input.text())

    def do_search(self, query: str) -> None:
        self._content_generation += 1
        generation = self._content_generation
        if self._search_worker and self._search_worker.isRunning():
            self._search_worker.request_cancel()
        if self._rec_worker and self._rec_worker.isRunning():
            self._rec_worker.request_cancel()

        query = (query or '').strip()
        if not query:
            self._loaded_recommendations = False
            self._load_recommendations()
            return
        self.hint_label.setText(f'搜尋「{query}」中…')
        self.progress_ring.show()
        self._clear_cards()

        async def search():
            async with self.backend_provider() as backend:
                return await backend.search_games(query)

        self._search_worker = AsyncWorker(search)
        self._search_worker.setProperty('generation', generation)
        self._search_worker.result.connect(self._on_search_worker_result)
        self._search_worker.error.connect(self._on_search_worker_error)
        start_registered_worker(self, self._search_worker)

    def _on_search_worker_result(self, games: List[Dict]) -> None:
        worker = self.sender()
        generation = (
            int(worker.property('generation'))
            if isinstance(worker, QObject)
            else -1
        )
        self._on_search_ok(games, generation)

    def _on_search_worker_error(self, error: object) -> None:
        worker = self.sender()
        generation = (
            int(worker.property('generation'))
            if isinstance(worker, QObject)
            else -1
        )
        self._on_search_err(error, generation)

    def _on_search_ok(
        self,
        games: List[Dict],
        generation: int,
    ) -> None:
        if generation != self._content_generation:
            return
        self.progress_ring.hide()
        if not games:
            self.hint_label.setText('未找到結果')
            return
        self.hint_label.setText(f'搜尋結果 ({len(games)})')
        self._render_cards(games)

    def _on_search_err(self, error: object, generation: int) -> None:
        if generation != self._content_generation:
            return
        self.progress_ring.hide()
        self.hint_label.setText(f'搜尋失敗: {_error_text(error)}')

    def _clear_cards(self) -> None:
        for card in self.cards:
            self.flow.removeWidget(card)
            card.deleteLater()
        self.cards.clear()

    def _render_cards(self, games: List[Dict]) -> None:
        self._clear_cards()
        self._refresh_installed_set()
        for game in games:
            card = GameCard(
                game,
                self.grid_widget,
                image_loader=ImageLoader.instance(runtime=self.runtime),
                header_provider=lambda appid: get_cached_header(
                    appid,
                    runtime=self.runtime,
                ),
            )
            card.install_clicked.connect(self._on_install)
            if card.appid in self._installed_appids:
                card.set_installed(True)
            self.flow.addWidget(card)
            self.cards.append(card)

    def _on_install(self, appid: str, name: str) -> None:
        appid = str(appid)
        if (
            appid in self._install_workers
            and self._install_workers[appid].isRunning()
        ):
            InfoBar.warning(
                title='已在入庫中',
                content=f'{name} (AppID {appid}) 正在處理中',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2000,
            )
            return

        target_card = next(
            (card for card in self.cards if card.appid == appid),
            None,
        )
        if target_card:
            target_card.set_busy(True)

        source = {0: 'auto', 1: 'ryuu', 2: 'sudama'}.get(
            self.source_combo.currentIndex(),
            'auto',
        )
        config = load_config(runtime=self.runtime)
        if source == 'ryuu' and not config.get('Ryuu_Auth_Key', '').strip():
            InfoBar.error(
                title='未設定 Ryuu Auth Key',
                content='請到設定頁面填寫 Ryuu auth_key，或改用「自動 / 僅 Sudama」',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )
            if target_card:
                target_card.set_busy(False)
            return

        InfoBar.info(
            title='正在入庫',
            content=f'{name} (AppID {appid})',
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2000,
        )

        async def install():
            async with self.backend_provider() as backend:
                ok, message = backend.initialize()
                if not ok:
                    return False, message
                if not backend.steam_path or not ost_installed(
                    backend.steam_path,
                    runtime=self.runtime,
                ):
                    return (
                        False,
                        'OpenSteamTool 尚未安裝；請先到「OST 管理」完成安裝',
                    )
                return await backend.install_game(appid, source=source)

        worker = AsyncWorker(install)
        worker.setProperty('appid', appid)
        worker.setProperty('game_name', name)
        worker.result.connect(self._on_install_worker_result)
        worker.error.connect(self._on_install_worker_error)
        self._install_workers[appid] = worker
        if not start_registered_worker(self, worker, f'lua:{appid}'):
            self._install_workers.pop(appid, None)
            if target_card:
                target_card.set_busy(False)
            InfoBar.warning(
                title='作業進行中',
                content=f'AppID {appid} 正由另一個作業處理',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2500,
            )

    def _install_worker_metadata(self) -> tuple[str, str]:
        worker = self.sender()
        if not isinstance(worker, QObject):
            return '', ''
        return (
            str(worker.property('appid') or ''),
            str(worker.property('game_name') or ''),
        )

    def _on_install_worker_result(self, result: object) -> None:
        appid, name = self._install_worker_metadata()
        self._on_install_done(result, appid, name)

    def _on_install_worker_error(self, error: object) -> None:
        appid, name = self._install_worker_metadata()
        self._on_install_done((False, _error_text(error)), appid, name)

    def _on_install_done(self, result, appid: str, name: str) -> None:
        appid = str(appid)
        self._install_workers.pop(appid, None)
        for card in self.cards:
            if card.appid == appid:
                card.set_busy(False)
                break
        ok, message = (
            result if isinstance(result, tuple) else (False, str(result))
        )
        if ok:
            was_already_installed = appid in self._installed_appids
            self._installed_appids.add(appid)
            for card in self.cards:
                if card.appid == appid:
                    card.set_installed(True)
            hint = (
                '已覆蓋既有 lua，需要重啟 Steam 才會更新'
                if was_already_installed
                else 'OST 已自動載入，無需重啟 Steam'
            )
            InfoBar.success(
                title='入庫成功',
                content=f'{name} - {message}\n{hint}',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        else:
            InfoBar.error(
                title='入庫失敗',
                content=f'{name} - {message}',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )


__all__ = ['HomePage']
