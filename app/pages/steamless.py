"""Steamless tool management and multi-file unpack page."""

from __future__ import annotations

from functools import partial
from pathlib import Path
import sys
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    GroupHeaderCardWidget,
    HyperlinkButton,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    PrimaryPushButton,
    ProgressBar,
    PushButton,
    ScrollArea,
    SwitchButton,
    TitleLabel,
)

from app.pages.common import resolve_backend_provider
from app.services.tasks import start_registered_worker as _start_registered_worker
from app.tasking import AsyncWorker, WorkerFailure
from app.workers import SteamlessUnpackWorker
from backend.ost_backend import (
    OSTBackend,
    STEAMLESS_DEFAULT_REPO,
    STEAMLESS_FLAGS,
    is_steamless_installed,
    load_config,
    version_newer,
)
from backend.runtime import BackendRuntime, resolve_runtime


def _error_text(error: object) -> str:
    if isinstance(error, WorkerFailure):
        return error.message or type(error.exception).__name__
    return str(error)


class SteamlessPage(ScrollArea):
    """Steamless 脫殼工具：管理 Steamless 安裝、選擇 exe、批次脫殼"""

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
        self.setObjectName('steamless')
        self.setWidgetResizable(True)
        self.setStyleSheet('QScrollArea { background: transparent; border: none; }')
        # 接受拖拉檔案
        self.setAcceptDrops(True)

        container = QWidget()
        container.setStyleSheet('background: transparent;')
        self.setWidget(container)
        self.main_layout = QVBoxLayout(container)
        self.main_layout.setContentsMargins(30, 24, 30, 24)
        self.main_layout.setSpacing(16)

        self.main_layout.addWidget(TitleLabel('Steamless 脫殼', container))
        sub = CaptionLabel('移除 Steam Stub DRM 包覆（atom0s/Steamless）', container)
        sub.setTextColor('#666', '#aaa')
        self.main_layout.addWidget(sub)

        # ---- 工具管理卡 ----
        self.tool_card = GroupHeaderCardWidget(container)
        self.tool_card.setTitle('Steamless 工具')
        self.tool_card.setBorderRadius(8)

        tool_status_widget = QWidget()
        tsl = QVBoxLayout(tool_status_widget)
        tsl.setContentsMargins(0, 0, 0, 0)
        self.tool_status_label = BodyLabel('正在檢測…')
        tsl.addWidget(self.tool_status_label)

        ver_row = QHBoxLayout()
        self.local_ver_label = BodyLabel('本地版本: 未知')
        self.latest_ver_label = BodyLabel('最新版本: 檢查中…')
        ver_row.addWidget(self.local_ver_label)
        ver_row.addSpacing(20)
        ver_row.addWidget(self.latest_ver_label)
        ver_row.addStretch(1)
        tsl.addLayout(ver_row)

        self.tool_progress = ProgressBar(tool_status_widget)
        self.tool_progress.setRange(0, 100)
        self.tool_progress.setValue(0)
        self.tool_progress.hide()
        tsl.addWidget(self.tool_progress)

        self.tool_progress_label = CaptionLabel('', tool_status_widget)
        self.tool_progress_label.setTextColor('#888', '#aaa')
        self.tool_progress_label.hide()
        tsl.addWidget(self.tool_progress_label)

        btn_row = QHBoxLayout()
        self.check_update_btn = PushButton('檢查更新')
        self.check_update_btn.setIcon(FluentIcon.SYNC)
        self.check_update_btn.clicked.connect(self.check_update)
        btn_row.addWidget(self.check_update_btn)
        self.install_tool_btn = PrimaryPushButton('安裝 / 更新')
        self.install_tool_btn.setIcon(FluentIcon.DOWNLOAD)
        self.install_tool_btn.clicked.connect(self.install_steamless)
        btn_row.addWidget(self.install_tool_btn)
        self.uninstall_tool_btn = PushButton('卸載')
        self.uninstall_tool_btn.setIcon(FluentIcon.DELETE)
        self.uninstall_tool_btn.clicked.connect(self.uninstall_steamless)
        btn_row.addWidget(self.uninstall_tool_btn)
        self.github_link = HyperlinkButton(f'https://github.com/{STEAMLESS_DEFAULT_REPO}', 'GitHub')
        btn_row.addWidget(self.github_link)
        btn_row.addStretch(1)
        tsl.addLayout(btn_row)

        self.tool_card.addGroup(
            FluentIcon.APPLICATION, '工具狀態',
            f'Steamless.CLI.exe 將下載到 {self.runtime.steamless_dir}，'
            'CLI 由 .NET Framework 4 執行',
            tool_status_widget,
        )
        self.main_layout.addWidget(self.tool_card)

        # ---- 檔案選擇卡 ----
        self.file_card = GroupHeaderCardWidget(container)
        self.file_card.setTitle('目標檔案')
        self.file_card.setBorderRadius(8)

        file_widget = QWidget()
        fl = QVBoxLayout(file_widget)
        fl.setContentsMargins(0, 0, 0, 0)
        hint = CaptionLabel('可從檔案總管拖拉 exe 進此視窗，或點下方按鈕選擇')
        hint.setTextColor('#888', '#aaa')
        fl.addWidget(hint)

        self.file_list = QListWidget(file_widget)
        self.file_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.file_list.setFixedHeight(120)
        self.file_list.setStyleSheet(
            'QListWidget { background: rgba(255,255,255,0.04); '
            'border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; padding: 4px; }'
        )
        fl.addWidget(self.file_list)

        file_btn_row = QHBoxLayout()
        self.add_file_btn = PushButton('新增 exe…')
        self.add_file_btn.setIcon(FluentIcon.ADD)
        self.add_file_btn.clicked.connect(self._on_add_files)
        file_btn_row.addWidget(self.add_file_btn)
        self.remove_file_btn = PushButton('移除選取')
        self.remove_file_btn.setIcon(FluentIcon.REMOVE_FROM)
        self.remove_file_btn.clicked.connect(self._on_remove_selected)
        file_btn_row.addWidget(self.remove_file_btn)
        self.clear_file_btn = PushButton('全部清除')
        self.clear_file_btn.setIcon(FluentIcon.DELETE)
        self.clear_file_btn.clicked.connect(lambda: self.file_list.clear())
        file_btn_row.addWidget(self.clear_file_btn)
        file_btn_row.addStretch(1)
        fl.addLayout(file_btn_row)

        self.file_card.addGroup(
            FluentIcon.FOLDER, '輸入', 'Steamless 會在原檔旁產生 <檔名>.unpacked.exe',
            file_widget,
        )
        self.main_layout.addWidget(self.file_card)

        # ---- 選項卡 ----
        self.opt_card = GroupHeaderCardWidget(container)
        self.opt_card.setTitle('CLI 選項')
        self.opt_card.setBorderRadius(8)

        self._opt_switches: Dict[str, SwitchButton] = {}
        for key, flag, default, desc in STEAMLESS_FLAGS:
            sw_widget = QWidget()
            sw_layout = QHBoxLayout(sw_widget)
            sw_layout.setContentsMargins(0, 0, 0, 0)
            sw = SwitchButton()
            sw.setChecked(default)
            sw_layout.addWidget(sw)
            self._opt_switches[key] = sw
            self.opt_card.addGroup(FluentIcon.SETTING, flag, desc, sw_widget)

        # 額外開關：脫殼後自動覆蓋原檔
        replace_widget = QWidget()
        rl = QHBoxLayout(replace_widget)
        rl.setContentsMargins(0, 0, 0, 0)
        self.replace_switch = SwitchButton()
        self.replace_switch.setChecked(False)
        rl.addWidget(self.replace_switch)
        self.opt_card.addGroup(
            FluentIcon.UPDATE, '自動覆蓋原檔',
            '脫殼成功後將原 exe 備份至 .bak[.N]，並以脫殼版取代原檔',
            replace_widget,
        )
        self.main_layout.addWidget(self.opt_card)

        # ---- 執行卡 ----
        self.run_card = CardWidget(container)
        self.run_card.setBorderRadius(8)
        rl = QVBoxLayout(self.run_card)
        rl.setContentsMargins(16, 12, 16, 12)
        rl.setSpacing(8)

        run_btn_row = QHBoxLayout()
        self.run_btn = PrimaryPushButton('開始脫殼')
        self.run_btn.setIcon(FluentIcon.PLAY)
        self.run_btn.clicked.connect(self._on_run)
        run_btn_row.addWidget(self.run_btn)
        self.cancel_btn = PushButton('取消')
        self.cancel_btn.setIcon(FluentIcon.CLOSE)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setEnabled(False)
        run_btn_row.addWidget(self.cancel_btn)
        self.clear_log_btn = PushButton('清空日誌')
        self.clear_log_btn.setIcon(FluentIcon.BROOM)
        self.clear_log_btn.clicked.connect(lambda: self.log_view.clear())
        run_btn_row.addWidget(self.clear_log_btn)
        self.open_output_btn = PushButton('開啟最近輸出位置')
        self.open_output_btn.setIcon(FluentIcon.FOLDER)
        self.open_output_btn.clicked.connect(self._on_open_output)
        self.open_output_btn.setEnabled(False)
        run_btn_row.addWidget(self.open_output_btn)
        run_btn_row.addStretch(1)
        rl.addLayout(run_btn_row)

        self.unpack_progress_label = CaptionLabel('')
        self.unpack_progress_label.setTextColor('#888', '#aaa')
        rl.addWidget(self.unpack_progress_label)

        self.log_view = QPlainTextEdit(self.run_card)
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(220)
        self.log_view.document().setMaximumBlockCount(2000)
        mono = QFont('Consolas')
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.log_view.setFont(mono)
        self.log_view.setStyleSheet(
            'QPlainTextEdit { background: #1a1a1d; color: #cfd8dc; '
            'border: 1px solid rgba(255,255,255,0.08); border-radius: 6px; padding: 8px; }'
        )
        self.log_view.setPlaceholderText('Steamless 輸出將顯示在此處…')
        rl.addWidget(self.log_view)
        self.main_layout.addWidget(self.run_card)

        self.main_layout.addStretch(1)

        # State
        self._latest_release: Optional[Dict] = None
        self._check_worker: Optional[AsyncWorker] = None
        self._install_worker: Optional[AsyncWorker] = None
        self._uninstall_worker: Optional[AsyncWorker] = None
        self._unpack_worker: Optional[SteamlessUnpackWorker] = None
        self._last_output_path: Optional[Path] = None

        QTimer.singleShot(0, self.refresh_status)
        QTimer.singleShot(500, self.check_update)

    # ---- 拖拉 ----
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if not e.mimeData().hasUrls():
            super().dropEvent(e)
            return
        added = 0
        for url in e.mimeData().urls():
            local = url.toLocalFile()
            if local and self._add_file_if_valid(local):
                added += 1
        if added:
            e.acceptProposedAction()

    # ---- 檔案管理 ----
    def _add_file_if_valid(self, path: str) -> bool:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return False
        if p.suffix.lower() != '.exe':
            return False
        # 去重
        existing = {self.file_list.item(i).text() for i in range(self.file_list.count())}
        if str(p) in existing:
            return False
        QListWidgetItem(str(p), self.file_list)
        return True

    def _on_add_files(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, '選擇 Steam exe', '', 'Steam game exe (*.exe);;所有檔案 (*.*)'
        )
        for f in files:
            self._add_file_if_valid(f)

    def _on_remove_selected(self):
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))

    # ---- 工具狀態 ----
    def showEvent(self, e):
        super().showEvent(e)
        self.refresh_status()

    def refresh_status(self):
        installed = is_steamless_installed(runtime=self.runtime)
        if installed:
            self.tool_status_label.setText('✓ Steamless 已安裝')
            self.tool_status_label.setToolTip(f'CLI: {self.runtime.steamless_cli}')
            self.run_btn.setEnabled(True)
            self.uninstall_tool_btn.setEnabled(True)
        else:
            self.tool_status_label.setText('✗ Steamless 尚未安裝，請點下方「安裝 / 更新」')
            self.tool_status_label.setToolTip(
                f'將部署到: {self.runtime.steamless_dir}'
            )
            self.run_btn.setEnabled(False)
            self.uninstall_tool_btn.setEnabled(False)
        cfg = load_config(runtime=self.runtime)
        local = cfg.get('installed_steamless_version', '') or '未知'
        self.local_ver_label.setText(f'本地版本: {local}')

    def check_update(self):
        if self._check_worker and self._check_worker.isRunning():
            return
        self.latest_ver_label.setText('最新版本: 檢查中…')
        self.check_update_btn.setEnabled(False)

        async def _fetch():
            async with self.backend_provider() as backend:
                return await backend.fetch_steamless_latest()

        self._check_worker = AsyncWorker(_fetch)
        self._check_worker.result.connect(self._on_update_checked)
        self._check_worker.error.connect(self._on_update_check_failed)
        if not _start_registered_worker(self, self._check_worker, 'steamless-release-check'):
            self.check_update_btn.setEnabled(True)

    def _on_update_checked(self, release):
        self.check_update_btn.setEnabled(True)
        if not release:
            self.latest_ver_label.setText('最新版本: 取得失敗')
            return
        self._latest_release = release
        tag = release.get('tag_name', '')
        self.latest_ver_label.setText(f'最新版本: {tag}')
        cfg = load_config(runtime=self.runtime)
        local = cfg.get('installed_steamless_version', '')
        if local and tag and not version_newer(tag, local):
            self.install_tool_btn.setText('重新安裝')
        else:
            self.install_tool_btn.setText('安裝 / 更新')

    def _on_update_check_failed(self, err: object):
        self.check_update_btn.setEnabled(True)
        self.latest_ver_label.setText(f'最新版本: 失敗 ({_error_text(err)[:40]})')

    # ---- 安裝 / 更新 ----
    def install_steamless(self):
        if self._install_worker and self._install_worker.isRunning():
            return

        original_text = self.install_tool_btn.text()
        self.install_tool_btn.setEnabled(False)
        self.install_tool_btn.setText('安裝中…')
        self.check_update_btn.setEnabled(False)
        self.uninstall_tool_btn.setEnabled(False)
        self.tool_progress.show()
        self.tool_progress.setValue(0)
        self.tool_progress_label.show()
        self.tool_progress_label.setText('準備中…')

        async def _install(report):
            def progress_cb(stage: str, cur: int, tot: int):
                if stage == 'fetch':
                    text = '取得 release 資訊…'
                    val = 5
                elif stage == 'download':
                    pct = int(cur * 90 / max(tot, 1)) if tot else 50
                    text = f'下載中 {cur // 1024} / {(tot or cur) // 1024} KB'
                    val = 5 + pct
                elif stage == 'install':
                    text = '解壓 Steamless 到應用程式資料目錄…'
                    val = 95 if cur == 0 else 100
                else:
                    text = stage
                    val = 50
                report((text, val))

            async with self.backend_provider() as backend:
                return await backend.install_or_update_steamless(progress_cb)

        self._install_worker = AsyncWorker(_install)
        self._install_worker.setProperty('original_text', original_text)
        self._install_worker.progress.connect(self._on_tool_install_progress)
        self._install_worker.result.connect(self._on_tool_install_result)
        self._install_worker.error.connect(self._on_tool_install_error)
        if not _start_registered_worker(
            self, self._install_worker, ('steamless-tool', 'steamless-cli')
        ):
            self._on_install_finished(
                (False, 'Steamless 正由另一個安裝、移除或脫殼作業使用'), original_text
            )

    def _tool_install_original_text(self) -> str:
        worker = self.sender()
        if isinstance(worker, QObject):
            return str(worker.property('original_text') or '安裝 / 更新')
        return '安裝 / 更新'

    def _on_tool_install_progress(self, value: object) -> None:
        if isinstance(value, tuple) and len(value) == 2:
            self._update_tool_progress(str(value[0]), int(value[1]))

    def _on_tool_install_result(self, result: object) -> None:
        self._on_install_finished(result, self._tool_install_original_text())

    def _on_tool_install_error(self, error: object) -> None:
        self._on_install_finished(
            (False, _error_text(error)), self._tool_install_original_text()
        )

    def _update_tool_progress(self, text: str, value: int):
        try:
            self.tool_progress.setValue(max(0, min(100, value)))
            self.tool_progress_label.setText(text)
        except Exception:
            pass

    def _on_install_finished(self, result, original_text: str):
        self.install_tool_btn.setEnabled(True)
        self.check_update_btn.setEnabled(True)
        self.uninstall_tool_btn.setEnabled(True)
        self.install_tool_btn.setText(original_text)

        ok, msg = result if isinstance(result, tuple) else (False, str(result))
        self.tool_progress.setValue(100 if ok else 0)
        self.tool_progress_label.setText(msg)
        if ok:
            InfoBar.success(title='Steamless 安裝完成', content=msg, parent=self,
                            position=InfoBarPosition.TOP, duration=5000)
        else:
            InfoBar.error(title='Steamless 安裝失敗', content=msg, parent=self,
                          position=InfoBarPosition.TOP, duration=6000)
        self.refresh_status()
        QTimer.singleShot(3000, self._hide_tool_progress)

    def _hide_tool_progress(self) -> None:
        self.tool_progress.hide()
        self.tool_progress_label.hide()

    # ---- 卸載 ----
    def uninstall_steamless(self):
        if self._uninstall_worker and self._uninstall_worker.isRunning():
            return
        confirm = MessageBox(
            '確認卸載 Steamless',
            f'將整個移除 {self.runtime.steamless_dir} 目錄。\n\n繼續？',
            self.window(),
        )
        if not confirm.exec():
            return

        self.uninstall_tool_btn.setEnabled(False)
        self.install_tool_btn.setEnabled(False)

        async def uninstall():
            backend = self.backend_provider()
            return backend.uninstall_steamless()

        self._uninstall_worker = AsyncWorker(uninstall)
        self._uninstall_worker.result.connect(self._on_uninstall_finished)
        self._uninstall_worker.error.connect(self._on_uninstall_worker_error)
        if not _start_registered_worker(
            self, self._uninstall_worker, ('steamless-tool', 'steamless-cli')
        ):
            self._on_uninstall_finished((False, 'Steamless 正由另一個作業使用'))

    def _on_uninstall_worker_error(self, error: object) -> None:
        self._on_uninstall_finished((False, _error_text(error)))

    def _on_uninstall_finished(self, result: object) -> None:
        self.uninstall_tool_btn.setEnabled(True)
        self.install_tool_btn.setEnabled(True)
        ok, msg = result if isinstance(result, tuple) else (False, str(result))
        if ok:
            InfoBar.success(title='已卸載', content=msg, parent=self,
                            position=InfoBarPosition.TOP, duration=4000)
        else:
            InfoBar.error(title='卸載失敗', content=msg, parent=self,
                          position=InfoBarPosition.TOP, duration=5000)
        self.refresh_status()

    # ---- 執行脫殼 ----
    def _collect_options(self) -> Dict[str, bool]:
        return {key: sw.isChecked() for key, sw in self._opt_switches.items()}

    def _collect_files(self) -> List[Path]:
        return [Path(self.file_list.item(i).text()) for i in range(self.file_list.count())]

    def _on_run(self):
        if self._unpack_worker and self._unpack_worker.isRunning():
            return
        if not is_steamless_installed(runtime=self.runtime):
            InfoBar.error(title='尚未安裝 Steamless', content='請先點「安裝 / 更新」下載 Steamless',
                          parent=self, position=InfoBarPosition.TOP, duration=4000)
            return
        files = self._collect_files()
        if not files:
            InfoBar.warning(title='未選擇檔案', content='請先選擇或拖拉至少一個 exe',
                            parent=self, position=InfoBarPosition.TOP, duration=3000)
            return

        self.run_btn.setEnabled(False)
        self.run_btn.setText('脫殼中…')
        self.cancel_btn.setEnabled(True)
        self.install_tool_btn.setEnabled(False)
        self.uninstall_tool_btn.setEnabled(False)
        self.add_file_btn.setEnabled(False)
        self.remove_file_btn.setEnabled(False)
        self.clear_file_btn.setEnabled(False)
        self.open_output_btn.setEnabled(False)
        self.unpack_progress_label.setText(f'共 {len(files)} 個檔案')
        self._last_output_path = None

        options = self._collect_options()
        replace_original = self.replace_switch.isChecked()
        self.log_view.appendPlainText(f'\n===== 開始批次：{len(files)} 個檔案 =====')
        self.log_view.appendPlainText(f'選項: {[k for k, v in options.items() if v] or "(全部關閉)"}'
                                     + (' | 自動覆蓋原檔' if replace_original else ''))

        self._unpack_worker = SteamlessUnpackWorker(
            files,
            options,
            replace_original=replace_original,
            runtime=self.runtime,
        )
        self._unpack_worker.line.connect(self._append_log)
        self._unpack_worker.one_done.connect(self._on_one_done)
        self._unpack_worker.all_done.connect(self._on_all_done)
        if not _start_registered_worker(self, self._unpack_worker, 'steamless-cli'):
            self._on_all_done(0, len(files))
            InfoBar.warning(
                title='Steamless 忙碌中', content='另一個安裝或脫殼作業正在使用 Steamless',
                parent=self, position=InfoBarPosition.TOP, duration=3500,
            )

    def _append_log(self, text: str):
        self.log_view.appendPlainText(text)
        # 自動捲到底
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_one_done(self, path: str, ok: bool, msg: str, out_path: str):
        if ok and out_path:
            self._last_output_path = Path(out_path)
            self.open_output_btn.setEnabled(True)

    def _on_all_done(self, success: int, fail: int):
        self.run_btn.setEnabled(True)
        self.run_btn.setText('開始脫殼')
        self.cancel_btn.setEnabled(False)
        self.install_tool_btn.setEnabled(True)
        self.uninstall_tool_btn.setEnabled(True)
        self.add_file_btn.setEnabled(True)
        self.remove_file_btn.setEnabled(True)
        self.clear_file_btn.setEnabled(True)
        self.unpack_progress_label.setText(f'完成：成功 {success}、失敗 {fail}')
        self._append_log(f'\n===== 全部完成：成功 {success}、失敗 {fail} =====')
        if fail == 0 and success > 0:
            InfoBar.success(title='脫殼完成', content=f'共 {success} 個檔案',
                            parent=self, position=InfoBarPosition.TOP, duration=4000)
        elif success > 0:
            InfoBar.warning(title='部分完成', content=f'成功 {success}、失敗 {fail}',
                            parent=self, position=InfoBarPosition.TOP, duration=5000)
        else:
            InfoBar.error(title='全部失敗', content=f'失敗 {fail}',
                          parent=self, position=InfoBarPosition.TOP, duration=6000)

    def _on_cancel(self):
        if self._unpack_worker and self._unpack_worker.isRunning():
            self._unpack_worker.cancel()
            self.run_btn.setText('取消中…')
            self.cancel_btn.setEnabled(False)
            self._append_log('[cancel] 已要求取消，將在當前檔案完成或被中斷後停止')

    def _on_open_output(self):
        if not self._last_output_path or not self._last_output_path.exists():
            return
        kwargs = {}
        if sys.platform == 'win32':
            import subprocess as _sp
            si = _sp.STARTUPINFO()
            si.dwFlags |= _sp.STARTF_USESHOWWINDOW
            si.wShowWindow = _sp.SW_HIDE
            kwargs['startupinfo'] = si
            kwargs['creationflags'] = _sp.CREATE_NO_WINDOW
            try:
                _sp.Popen(['explorer', '/select,', str(self._last_output_path)], **kwargs)
            except Exception:
                pass


__all__ = ['SteamlessPage']
