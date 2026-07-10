"""Installed Lua library page with individual and batch update actions."""

from __future__ import annotations

from functools import partial
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    PrimaryPushButton,
    PushButton,
    ScrollArea,
    TitleLabel,
)

from app.pages.common import error_text as _error_text, resolve_backend_provider
from app.services.image_loader import ImageLoader
from app.services.tasks import start_registered_worker as _start_registered_worker
from app.tasking import AsyncWorker
from backend.ost_backend import (
    OSTBackend,
    get_cached_header,
    get_cached_name,
    load_config,
    ost_installed,
)
from backend.runtime import BackendRuntime, resolve_runtime

class InstalledPage(ScrollArea):
    def __init__(
        self,
        parent=None,
        *,
        backend_provider=None,
        runtime: BackendRuntime | None = None,
    ):
        super().__init__(parent)
        self.runtime = resolve_runtime(runtime)
        self.backend_provider = (
            backend_provider
            if backend_provider is not None
            else resolve_backend_provider(
                self,
                partial(OSTBackend, runtime=self.runtime),
            )
        )
        self.setObjectName('installed')
        self.setWidgetResizable(True)
        self.setStyleSheet('QScrollArea { background: transparent; border: none; }')

        container = QWidget()
        container.setStyleSheet('background: transparent;')
        self.setWidget(container)
        self.main_layout = QVBoxLayout(container)
        self.main_layout.setContentsMargins(30, 24, 30, 24)
        self.main_layout.setSpacing(12)

        header_row = QHBoxLayout()
        title = TitleLabel('已入庫遊戲', container)
        header_row.addWidget(title)
        header_row.addStretch(1)
        self.source_combo = ComboBox(container)
        self.source_combo.addItems(['自動 (Ryuu → Sudama)', '僅 Ryuu', '僅 Sudama'])
        self.source_combo.setFixedWidth(180)
        self.reload_settings()
        header_row.addWidget(self.source_combo)
        self.update_all_btn = PrimaryPushButton('全部更新 Lua', container)
        self.update_all_btn.setIcon(FluentIcon.UPDATE)
        self.update_all_btn.clicked.connect(self._on_update_all)
        header_row.addWidget(self.update_all_btn)
        self.refresh_btn = PushButton('重新整理', container)
        self.refresh_btn.setIcon(FluentIcon.SYNC)
        self.refresh_btn.clicked.connect(self.refresh)
        header_row.addWidget(self.refresh_btn)
        self.main_layout.addLayout(header_row)

        self.hint = CaptionLabel('', container)
        self.hint.setTextColor('#888', '#aaa')
        self.main_layout.addWidget(self.hint)

        self.list_widget = QWidget(container)
        self.list_widget.setStyleSheet('background: transparent;')
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(8)
        self.main_layout.addWidget(self.list_widget)
        self.main_layout.addStretch(1)

        self._rows: List[QWidget] = []
        # appid -> widget refs，給非同步更新用
        self._title_labels: Dict[str, BodyLabel] = {}
        self._cover_labels: Dict[str, QLabel] = {}
        self._update_buttons: Dict[str, PushButton] = {}
        self._installed_appids: List[str] = []
        self._name_worker: Optional[AsyncWorker] = None
        self._name_generation = 0
        self._update_workers: Dict[str, AsyncWorker] = {}
        self._delete_workers: Dict[str, AsyncWorker] = {}
        self._batch_worker: Optional[AsyncWorker] = None
        QTimer.singleShot(0, self.refresh)

    def reload_settings(self) -> None:
        source = load_config(runtime=self.runtime).get('default_source', 'auto')
        self.source_combo.setCurrentIndex({'auto': 0, 'ryuu': 1, 'sudama': 2}.get(source, 0))

    def _current_source(self) -> str:
        return {0: 'auto', 1: 'ryuu', 2: 'sudama'}.get(
            self.source_combo.currentIndex(), 'auto'
        )

    def _source_is_ready(self, source: str) -> bool:
        if source != 'ryuu' or load_config(runtime=self.runtime).get(
            'Ryuu_Auth_Key', ''
        ).strip():
            return True
        InfoBar.error(
            title='未設定 Ryuu Auth Key',
            content='請到設定頁面填寫 Ryuu auth_key，或改用「自動 / 僅 Sudama」',
            parent=self,
            position=InfoBarPosition.TOP,
            duration=4000,
        )
        return False

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()

    def _clear_rows(self):
        for r in self._rows:
            self.list_layout.removeWidget(r)
            r.deleteLater()
        self._rows.clear()
        self._title_labels.clear()
        self._cover_labels.clear()
        self._update_buttons.clear()

    def refresh(self):
        self._clear_rows()
        backend = self.backend_provider()
        ok, err = backend.initialize()
        if not ok:
            self.hint.setText(f'未能初始化: {err}')
            return
        rows = backend.list_installed()
        self._installed_appids = [str(item['appid']) for item in rows]
        self.update_all_btn.setEnabled(bool(rows) and not (
            self._batch_worker and self._batch_worker.isRunning()
        ))
        if not rows:
            self.hint.setText(f'config/lua 內尚無已入庫遊戲 ({backend.lua_dir})')
            return
        self.hint.setText(f'共 {len(rows)} 項 ({backend.lua_dir})')
        for item in rows:
            row = self._make_row(item['appid'], item['path'])
            self.list_layout.addWidget(row)
            self._rows.append(row)

        # 非同步抓取缺失的標題 / header URL
        missing = [
            item['appid'] for item in rows
            if not get_cached_name(item['appid'], runtime=self.runtime)
            or not get_cached_header(item['appid'], runtime=self.runtime)
        ]
        if missing:
            self._kick_off_name_fetch(missing)

    def _kick_off_name_fetch(self, appids: List[str]):
        self._name_generation += 1
        generation = self._name_generation
        if self._name_worker and self._name_worker.isRunning():
            self._name_worker.request_cancel()

        async def _fetch():
            async with self.backend_provider() as backend:
                return await backend.fetch_names_for(appids)

        self._name_worker = AsyncWorker(_fetch)
        self._name_worker.setProperty('generation', generation)
        self._name_worker.result.connect(self._on_names_worker_result)
        _start_registered_worker(self, self._name_worker)

    def _on_names_worker_result(self, result: Dict[str, Dict[str, str]]) -> None:
        worker = self.sender()
        generation = int(worker.property('generation')) if isinstance(worker, QObject) else -1
        self._on_names_fetched(result, generation)

    def _on_names_fetched(self, info_map: Dict[str, Dict[str, str]], generation: int):
        if generation != self._name_generation:
            return
        for appid, info in (info_map or {}).items():
            appid = str(appid)
            name = info.get('name', '') if isinstance(info, dict) else ''
            header = info.get('header_image', '') if isinstance(info, dict) else ''
            lbl = self._title_labels.get(appid)
            if lbl and name:
                lbl.setText(name)
            # 若有真實 header URL 且還沒載入，主動過載對應封面
            cover = self._cover_labels.get(appid) if hasattr(self, '_cover_labels') else None
            if cover and header:
                ImageLoader.instance(runtime=self.runtime).load(
                    header,
                    lambda pix, c=cover: c.setPixmap(pix.scaled(
                        c.size(),
                        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation
                    )),
                    appid=appid
                )

    def _make_row(self, appid: str, path: str) -> QWidget:
        appid = str(appid)
        card = CardWidget(self.list_widget)
        card.setBorderRadius(8)
        card.setFixedHeight(72)
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(12)

        # 封面
        cover = QLabel(card)
        cover.setFixedSize(110, 52)
        cover.setStyleSheet('background: #2a2a2e; border-radius: 4px;')
        cover.setScaledContents(True)
        self._cover_labels[appid] = cover
        cached_header = get_cached_header(appid, runtime=self.runtime)
        primary = cached_header or f'https://cdn.cloudflare.steamstatic.com/steam/apps/{appid}/header.jpg'
        ImageLoader.instance(runtime=self.runtime).load(
            primary,
            lambda pix, c=cover: c.setPixmap(pix.scaled(
                c.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation
            )),
            appid=appid,
        )
        layout.addWidget(cover)

        # 標題 + AppID 副標
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        cached_title = get_cached_name(appid, runtime=self.runtime) or '載入中…'
        title_label = BodyLabel(cached_title, card)
        title_font = title_label.font()
        title_font.setWeight(QFont.Weight.DemiBold)
        title_label.setFont(title_font)
        text_col.addWidget(title_label)
        self._title_labels[appid] = title_label

        sub_label = CaptionLabel(f'AppID: {appid}', card)
        sub_label.setTextColor('#888', '#aaa')
        text_col.addWidget(sub_label)
        layout.addLayout(text_col)

        layout.addStretch(1)

        update_btn = PrimaryPushButton('更新 Lua', card)
        update_btn.setIcon(FluentIcon.UPDATE)
        update_btn.clicked.connect(lambda _checked=False, a=appid: self._on_update(a))
        self._update_buttons[appid] = update_btn
        layout.addWidget(update_btn)

        del_btn = PushButton('刪除', card)
        del_btn.setIcon(FluentIcon.DELETE)
        del_btn.clicked.connect(lambda _checked=False, a=appid: self._on_delete(a))
        layout.addWidget(del_btn)
        return card

    def _on_update(self, appid: str) -> None:
        appid = str(appid)
        if appid in self._update_workers and self._update_workers[appid].isRunning():
            return
        source = self._current_source()
        if not self._source_is_ready(source):
            return

        button = self._update_buttons.get(appid)
        if button:
            button.setEnabled(False)
            button.setText('更新中…')

        async def update_one():
            async with self.backend_provider() as backend:
                ok, message = backend.initialize()
                if not ok:
                    return False, message
                if not backend.steam_path or not ost_installed(
                    backend.steam_path,
                    runtime=self.runtime,
                ):
                    return False, 'OpenSteamTool 尚未安裝；請先到「OST 管理」完成安裝'
                return await backend.install_game(appid, source=source)

        worker = AsyncWorker(update_one)
        worker.setProperty('appid', appid)
        worker.result.connect(self._on_update_worker_result)
        worker.error.connect(self._on_update_worker_error)
        self._update_workers[appid] = worker
        if not _start_registered_worker(self, worker, f'lua:{appid}'):
            self._update_workers.pop(appid, None)
            self._reset_update_button(appid)
            InfoBar.warning(
                title='作業進行中',
                content=f'AppID {appid} 正由另一個作業處理',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2500,
            )

    def _update_worker_appid(self) -> str:
        worker = self.sender()
        return str(worker.property('appid') or '') if isinstance(worker, QObject) else ''

    def _on_update_worker_result(self, result: object) -> None:
        self._on_update_done(self._update_worker_appid(), result)

    def _on_update_worker_error(self, error: object) -> None:
        self._on_update_done(
            self._update_worker_appid(), (False, _error_text(error))
        )

    def _reset_update_button(self, appid: str) -> None:
        button = self._update_buttons.get(str(appid))
        if button:
            button.setEnabled(True)
            button.setText('更新 Lua')

    def _on_update_done(self, appid: str, result: object) -> None:
        self._update_workers.pop(str(appid), None)
        self._reset_update_button(appid)
        ok, message = result if isinstance(result, tuple) else (False, str(result))
        title = 'Lua 更新成功' if ok else 'Lua 更新失敗'
        content = f'AppID {appid} - {message}'
        if ok:
            content += '\n已覆蓋既有 Lua，請重啟 Steam 套用更新'
            InfoBar.success(
                title=title, content=content, parent=self,
                position=InfoBarPosition.TOP, duration=5000,
            )
        else:
            InfoBar.error(
                title=title, content=content, parent=self,
                position=InfoBarPosition.TOP, duration=5000,
            )

    def _on_update_all(self) -> None:
        appids = list(self._installed_appids)
        if not appids or (self._batch_worker and self._batch_worker.isRunning()):
            return
        source = self._current_source()
        if not self._source_is_ready(source):
            return

        self.update_all_btn.setEnabled(False)
        self.update_all_btn.setText('批次更新中…')
        for button in self._update_buttons.values():
            button.setEnabled(False)

        async def update_all(report):
            results: Dict[str, tuple[bool, str]] = {}
            async with self.backend_provider() as backend:
                ok, message = backend.initialize()
                if not ok:
                    return {appid: (False, message) for appid in appids}
                if not backend.steam_path or not ost_installed(
                    backend.steam_path,
                    runtime=self.runtime,
                ):
                    message = 'OpenSteamTool 尚未安裝；請先到「OST 管理」完成安裝'
                    return {appid: (False, message) for appid in appids}
                total = len(appids)
                for index, appid in enumerate(appids, 1):
                    report((index, total, appid))
                    results[appid] = await backend.install_game(appid, source=source)
            return results

        worker = AsyncWorker(update_all)
        worker.progress.connect(self._on_batch_progress)
        worker.result.connect(self._on_batch_done)
        worker.error.connect(self._on_batch_worker_error)
        self._batch_worker = worker
        resources = [f'lua:{appid}' for appid in appids]
        if not _start_registered_worker(self, worker, resources):
            self._batch_worker = None
            self._finish_batch_ui()
            InfoBar.warning(
                title='作業進行中',
                content='部分遊戲正由另一個作業處理，請稍後再試',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )

    def _on_batch_worker_error(self, error: object) -> None:
        self._on_batch_failed(_error_text(error))

    def _on_batch_progress(self, progress: object) -> None:
        if isinstance(progress, tuple) and len(progress) == 3:
            index, total, appid = progress
            self.hint.setText(f'批次更新 {index}/{total}：AppID {appid}')

    def _finish_batch_ui(self) -> None:
        self.update_all_btn.setEnabled(bool(self._installed_appids))
        self.update_all_btn.setText('全部更新 Lua')
        for appid in self._update_buttons:
            self._reset_update_button(appid)

    def _on_batch_done(self, results: object) -> None:
        self._batch_worker = None
        self._finish_batch_ui()
        result_map = results if isinstance(results, dict) else {}
        succeeded = sum(1 for ok, _message in result_map.values() if ok)
        failed = len(result_map) - succeeded
        self.hint.setText(f'批次更新完成：成功 {succeeded}、失敗 {failed}')
        if failed == 0 and succeeded:
            InfoBar.success(
                title='Lua 批次更新完成',
                content=f'成功更新 {succeeded} 項；請重啟 Steam 套用更新',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        else:
            failures = [
                f'{appid}: {message}'
                for appid, (ok, message) in result_map.items()
                if not ok
            ]
            details = '\n'.join(failures[:3])
            if len(failures) > 3:
                details += f'\n…另有 {len(failures) - 3} 項失敗'
            InfoBar.warning(
                title='Lua 批次更新完成',
                content=f'成功 {succeeded}、失敗 {failed}' + (f'\n{details}' if details else ''),
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )

    def _on_batch_failed(self, message: str) -> None:
        self._batch_worker = None
        self._finish_batch_ui()
        self.hint.setText(f'批次更新失敗: {message}')
        InfoBar.error(
            title='Lua 批次更新失敗', content=message, parent=self,
            position=InfoBarPosition.TOP, duration=6000,
        )

    def _on_delete(self, appid: str):
        if appid in self._delete_workers and self._delete_workers[appid].isRunning():
            return
        m = MessageBox(
            '確定刪除？',
            f'將從 config/lua 刪除 {appid}.lua\n\n'
            f'注意：OST 不會 hot reload 刪除動作，重啟 Steam 後遊戲才會從圖書館移除。',
            self.window(),
        )
        if m.exec():
            async def remove_one():
                backend = self.backend_provider()
                ok, error = backend.initialize()
                if not ok:
                    return False, error
                if backend.remove_installed(appid):
                    return True, ''
                return False, '後端拒絕移除或檔案驗證失敗'

            worker = AsyncWorker(remove_one)
            worker.setProperty('appid', appid)
            worker.result.connect(self._on_delete_worker_result)
            worker.error.connect(self._on_delete_worker_error)
            self._delete_workers[appid] = worker
            if not _start_registered_worker(self, worker, f'lua:{appid}'):
                self._delete_workers.pop(appid, None)
                InfoBar.warning(
                    title='作業進行中', content=f'AppID {appid} 正由另一個作業處理',
                    parent=self, position=InfoBarPosition.TOP, duration=2500,
                )

    def _delete_worker_appid(self) -> str:
        worker = self.sender()
        return str(worker.property('appid') or '') if isinstance(worker, QObject) else ''

    def _on_delete_worker_result(self, result: object) -> None:
        self._on_delete_done(self._delete_worker_appid(), result)

    def _on_delete_worker_error(self, error: object) -> None:
        self._on_delete_done(
            self._delete_worker_appid(), (False, _error_text(error))
        )

    def _on_delete_done(self, appid: str, result: object) -> None:
        self._delete_workers.pop(str(appid), None)
        ok, error = result if isinstance(result, tuple) else (False, str(result))
        if ok:
            InfoBar.success(
                title='已刪除',
                content=f'AppID {appid}（重啟 Steam 後從圖書館移除）',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            self.refresh()
        else:
            InfoBar.error(
                title='刪除失敗', content=error or '未知錯誤', parent=self,
                position=InfoBarPosition.TOP, duration=3000,
            )


__all__ = ['InstalledPage']
