"""KRKRZ patch deployment page."""

from __future__ import annotations

from pathlib import Path
import sys
from typing import List, Optional

from PyQt6.QtCore import QObject, Qt, QTimer
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
    PushButton,
    ScrollArea,
    SwitchButton,
    TitleLabel,
)

from app.services.tasks import start_registered_worker as _start_registered_worker
from app.workers import KrkrzWorker
from backend.ost_backend import (
    KRKRZ_REPO,
    bundled_krkrz_dll,
    detect_krkrz_status,
    is_steamless_installed,
)
from backend.runtime import BackendRuntime, resolve_runtime


class KrkrzPage(ScrollArea):
    """Deploy the bundled Version.dll patch to selected game directories."""

    def __init__(
        self,
        parent=None,
        *,
        runtime: BackendRuntime | None = None,
    ):
        super().__init__(parent)
        self.runtime = resolve_runtime(runtime)
        self.setObjectName('krkrz')
        self.setWidgetResizable(True)
        self.setStyleSheet(
            'QScrollArea { background: transparent; border: none; }'
        )
        self.setAcceptDrops(True)

        container = QWidget()
        container.setStyleSheet('background: transparent;')
        self.setWidget(container)
        self.main_layout = QVBoxLayout(container)
        self.main_layout.setContentsMargins(30, 24, 30, 24)
        self.main_layout.setSpacing(16)

        self.main_layout.addWidget(TitleLabel('KRKRZ 補丁', container))
        subtitle = CaptionLabel(
            '部署 Fuck_Cxdec_Check 的 Version.dll，繞過 KRKRZ 引擎完整性檢測',
            container,
        )
        subtitle.setTextColor('#666', '#aaa')
        self.main_layout.addWidget(subtitle)

        self.res_card = GroupHeaderCardWidget(container)
        self.res_card.setTitle('資源狀態')
        self.res_card.setBorderRadius(8)
        resource_widget = QWidget()
        resource_layout = QVBoxLayout(resource_widget)
        resource_layout.setContentsMargins(0, 0, 0, 0)
        self.res_status_label = BodyLabel('正在檢測…')
        resource_layout.addWidget(self.res_status_label)
        resource_button_row = QHBoxLayout()
        self.github_link = HyperlinkButton(
            f'https://github.com/{KRKRZ_REPO}',
            'GitHub 原專案',
        )
        resource_button_row.addWidget(self.github_link)
        resource_button_row.addStretch(1)
        resource_layout.addLayout(resource_button_row)
        self.res_card.addGroup(
            FluentIcon.APPLICATION,
            'Version.dll',
            '內嵌於 IceHub 中，無需額外下載',
            resource_widget,
        )
        self.main_layout.addWidget(self.res_card)

        self.target_card = GroupHeaderCardWidget(container)
        self.target_card.setTitle('目標')
        self.target_card.setBorderRadius(8)
        target_widget = QWidget()
        target_layout = QVBoxLayout(target_widget)
        target_layout.setContentsMargins(0, 0, 0, 0)
        hint = CaptionLabel(
            '拖拉遊戲主程式 exe 進此視窗。Version.dll 將部署到 exe 同層目錄。'
        )
        hint.setTextColor('#888', '#aaa')
        target_layout.addWidget(hint)

        self.target_list = QListWidget(target_widget)
        self.target_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        self.target_list.setFixedHeight(140)
        self.target_list.setStyleSheet(
            'QListWidget { background: rgba(255,255,255,0.04); '
            'border: 1px solid rgba(255,255,255,0.08); '
            'border-radius: 6px; padding: 4px; }'
        )
        target_layout.addWidget(self.target_list)

        target_button_row = QHBoxLayout()
        self.add_exe_btn = PushButton('新增遊戲 exe…')
        self.add_exe_btn.setIcon(FluentIcon.ADD)
        self.add_exe_btn.clicked.connect(self._on_add_exe)
        target_button_row.addWidget(self.add_exe_btn)
        self.remove_target_btn = PushButton('移除選取')
        self.remove_target_btn.setIcon(FluentIcon.REMOVE_FROM)
        self.remove_target_btn.clicked.connect(self._on_remove_selected_target)
        target_button_row.addWidget(self.remove_target_btn)
        self.clear_target_btn = PushButton('全部清除')
        self.clear_target_btn.setIcon(FluentIcon.DELETE)
        self.clear_target_btn.clicked.connect(lambda: self.target_list.clear())
        target_button_row.addWidget(self.clear_target_btn)
        self.refresh_target_btn = PushButton('重新偵測')
        self.refresh_target_btn.setIcon(FluentIcon.SYNC)
        self.refresh_target_btn.clicked.connect(self._refresh_target_states)
        target_button_row.addWidget(self.refresh_target_btn)
        target_button_row.addStretch(1)
        target_layout.addLayout(target_button_row)
        self.target_card.addGroup(
            FluentIcon.FOLDER,
            '目標清單',
            '可同時部署多個遊戲。狀態欄顯示 KRKRZ 引擎跡象與既有部署狀態',
            target_widget,
        )
        self.main_layout.addWidget(self.target_card)

        self.opt_card = GroupHeaderCardWidget(container)
        self.opt_card.setTitle('選項')
        self.opt_card.setBorderRadius(8)
        backup_widget = QWidget()
        backup_layout = QHBoxLayout(backup_widget)
        backup_layout.setContentsMargins(0, 0, 0, 0)
        self.backup_switch = SwitchButton()
        self.backup_switch.setChecked(True)
        backup_layout.addWidget(self.backup_switch)
        self.opt_card.addGroup(
            FluentIcon.SAVE,
            '備份既有 version.dll',
            '目標目錄已存在 version.dll 時備份為 version.dll.bak[.N]'
            '（保留遊戲原本可能的合法 dll）',
            backup_widget,
        )

        restore_widget = QWidget()
        restore_layout = QHBoxLayout(restore_widget)
        restore_layout.setContentsMargins(0, 0, 0, 0)
        self.restore_switch = SwitchButton()
        self.restore_switch.setChecked(True)
        self.restore_switch.setEnabled(False)
        restore_layout.addWidget(self.restore_switch)
        self.opt_card.addGroup(
            FluentIcon.HISTORY,
            '移除時還原備份',
            '安全機制固定啟用：只還原 ownership manifest '
            '或嚴格驗證過的唯一原始 DLL',
            restore_widget,
        )

        steamless_widget = QWidget()
        steamless_layout = QHBoxLayout(steamless_widget)
        steamless_layout.setContentsMargins(0, 0, 0, 0)
        self.use_steamless_switch = SwitchButton()
        self.use_steamless_switch.setChecked(False)
        steamless_layout.addWidget(self.use_steamless_switch)
        self.opt_card.addGroup(
            FluentIcon.BROOM,
            '先用 Steamless 脫殼並覆蓋原檔',
            '對選取的 exe 先跑 Steamless（無 SteamStub 會自動跳過），'
            '原檔備份至 .bak[.N] 後以脫殼版取代',
            steamless_widget,
        )
        self.main_layout.addWidget(self.opt_card)

        self.run_card = CardWidget(container)
        self.run_card.setBorderRadius(8)
        run_layout = QVBoxLayout(self.run_card)
        run_layout.setContentsMargins(16, 12, 16, 12)
        run_layout.setSpacing(8)
        run_button_row = QHBoxLayout()
        self.deploy_btn = PrimaryPushButton('部署補丁')
        self.deploy_btn.setIcon(FluentIcon.DOWNLOAD)
        self.deploy_btn.clicked.connect(self._on_deploy)
        run_button_row.addWidget(self.deploy_btn)
        self.remove_btn = PushButton('移除補丁')
        self.remove_btn.setIcon(FluentIcon.DELETE)
        self.remove_btn.clicked.connect(self._on_remove)
        run_button_row.addWidget(self.remove_btn)
        self.open_dir_btn = PushButton('開啟所選目錄')
        self.open_dir_btn.setIcon(FluentIcon.FOLDER)
        self.open_dir_btn.clicked.connect(self._on_open_selected_dir)
        run_button_row.addWidget(self.open_dir_btn)
        self.clear_log_btn = PushButton('清空日誌')
        self.clear_log_btn.setIcon(FluentIcon.BROOM)
        self.clear_log_btn.clicked.connect(lambda: self.log_view.clear())
        run_button_row.addWidget(self.clear_log_btn)
        run_button_row.addStretch(1)
        run_layout.addLayout(run_button_row)

        self.log_view = QPlainTextEdit(self.run_card)
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(180)
        self.log_view.document().setMaximumBlockCount(2000)
        monospace = QFont('Consolas')
        monospace.setStyleHint(QFont.StyleHint.Monospace)
        self.log_view.setFont(monospace)
        self.log_view.setStyleSheet(
            'QPlainTextEdit { background: #1a1a1d; color: #cfd8dc; '
            'border: 1px solid rgba(255,255,255,0.08); '
            'border-radius: 6px; padding: 8px; }'
        )
        self.log_view.setPlaceholderText('部署/移除結果將顯示在此處…')
        run_layout.addWidget(self.log_view)
        self.main_layout.addWidget(self.run_card)
        self.main_layout.addStretch(1)

        self._worker: Optional[KrkrzWorker] = None
        QTimer.singleShot(0, self._refresh_resource_status)

    def _refresh_resource_status(self):
        dll = bundled_krkrz_dll(runtime=self.runtime)
        if dll and dll.exists():
            size_kb = dll.stat().st_size // 1024
            self.res_status_label.setText(
                f'✓ Version.dll 已內嵌（{size_kb} KB）'
            )
            self.res_status_label.setToolTip(str(dll))
            self.deploy_btn.setEnabled(
                not (self._worker and self._worker.isRunning())
            )
        else:
            self.res_status_label.setText(
                '✗ 找不到內嵌的 Version.dll，請重新打包 IceHub'
            )
            self.res_status_label.setToolTip('')
            self.deploy_btn.setEnabled(False)

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_resource_status()
        self._refresh_target_states()

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if not event.mimeData().hasUrls():
            super().dropEvent(event)
            return
        added = 0
        rejected = 0
        for url in event.mimeData().urls():
            local = url.toLocalFile()
            if not local:
                continue
            path = Path(local)
            if path.is_file() and path.suffix.lower() == '.exe':
                if self._add_target_exe(path):
                    added += 1
            else:
                rejected += 1
        if rejected and not added:
            InfoBar.warning(
                title='只接受遊戲主程式 exe',
                content='請拖拉遊戲主程式 (.exe)，不接受資料夾或其他類型檔案',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3500,
            )
        if added:
            event.acceptProposedAction()

    def _add_target_exe(self, executable: Path) -> bool:
        executable = executable.resolve()
        if (
            not executable.exists()
            or not executable.is_file()
            or executable.suffix.lower() != '.exe'
        ):
            return False
        existing = {
            self.target_list.item(index).data(Qt.ItemDataRole.UserRole)
            for index in range(self.target_list.count())
        }
        if str(executable) in existing:
            return False
        item = QListWidgetItem(self.target_list)
        item.setData(Qt.ItemDataRole.UserRole, str(executable))
        self._update_item_display(item, executable)
        return True

    def _update_item_display(
        self,
        item: QListWidgetItem,
        executable: Path,
    ):
        status = detect_krkrz_status(executable.parent, runtime=self.runtime)
        tags = [
            'KRKRZ ✓' if status['looks_like_krkrz'] else 'KRKRZ ✗'
        ]
        if status['deployed']:
            tags.append('已部署')
        if status['has_backup']:
            tags.append('有備份')
        item.setText(f'{executable}    [{", ".join(tags)}]')

    def _refresh_target_states(self):
        for index in range(self.target_list.count()):
            item = self.target_list.item(index)
            executable = Path(item.data(Qt.ItemDataRole.UserRole))
            self._update_item_display(item, executable)

    def _on_add_exe(self):
        files, _selected_filter = QFileDialog.getOpenFileNames(
            self,
            '選擇 KRKRZ 遊戲主程式 exe',
            '',
            '遊戲主程式 (*.exe);;所有檔案 (*.*)',
        )
        for file in files:
            self._add_target_exe(Path(file))

    def _on_remove_selected_target(self):
        for item in self.target_list.selectedItems():
            self.target_list.takeItem(self.target_list.row(item))

    def _collect_exes(self) -> List[Path]:
        return [
            Path(
                self.target_list.item(index).data(Qt.ItemDataRole.UserRole)
            )
            for index in range(self.target_list.count())
        ]

    def _ensure_no_worker(self) -> bool:
        if self._worker and self._worker.isRunning():
            InfoBar.warning(
                title='作業進行中',
                content='請等待目前的部署/移除完成',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return False
        return True

    def _on_deploy(self):
        if not self._ensure_no_worker():
            return
        if not bundled_krkrz_dll(runtime=self.runtime):
            InfoBar.error(
                title='缺少 Version.dll',
                content='內嵌資源遺失，請重新打包 IceHub',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )
            return
        executables = self._collect_exes()
        if not executables:
            InfoBar.warning(
                title='未選擇目標',
                content='請先新增或拖拉至少一個遊戲主程式 exe',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        if (
            self.use_steamless_switch.isChecked()
            and not is_steamless_installed(runtime=self.runtime)
        ):
            InfoBar.warning(
                title='Steamless 未安裝',
                content='已勾選「先用 Steamless 脫殼」但 Steamless 尚未安裝，'
                '請先到「Steamless 脫殼」分頁安裝',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
            return
        self._start_worker(executables, 'deploy')

    def _on_remove(self):
        if not self._ensure_no_worker():
            return
        executables = self._collect_exes()
        if not executables:
            InfoBar.warning(
                title='未選擇目標',
                content='請先新增或拖拉至少一個遊戲主程式 exe',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3000,
            )
            return
        directories = sorted({executable.parent for executable in executables})
        confirm = MessageBox(
            '確認移除補丁',
            f'將從以下 {len(directories)} 個遊戲目錄移除 Version.dll：\n'
            + '\n'.join(f'  • {directory}' for directory in directories[:5])
            + (
                f'\n  • ... 共 {len(directories)} 個'
                if len(directories) > 5
                else ''
            )
            + '\n\n移除時會依 ownership manifest 安全還原原始 Version.dll。',
            self.window(),
        )
        if not confirm.exec():
            return
        self._start_worker(executables, 'remove')

    def _start_worker(self, exes: List[Path], mode: str):
        self.deploy_btn.setEnabled(False)
        self.remove_btn.setEnabled(False)
        self.add_exe_btn.setEnabled(False)
        self.remove_target_btn.setEnabled(False)
        self.clear_target_btn.setEnabled(False)

        action_label = '部署' if mode == 'deploy' else '移除'
        use_steamless = (
            self.use_steamless_switch.isChecked() and mode == 'deploy'
        )
        self.log_view.appendPlainText(
            f'\n===== 開始{action_label}：{len(exes)} 個遊戲'
            + ('（含 Steamless 預處理）' if use_steamless else '')
            + ' ====='
        )

        self._worker = KrkrzWorker(
            exes,
            mode,
            backup_existing=self.backup_switch.isChecked(),
            restore_backup=self.restore_switch.isChecked(),
            use_steamless=use_steamless,
            runtime=self.runtime,
        )
        self._worker.setProperty('action_label', action_label)
        self._worker.line.connect(self._append_log)
        self._worker.all_done.connect(self._on_worker_all_done)
        resources = ['krkrz-files']
        if use_steamless:
            resources.append('steamless-cli')
        if not _start_registered_worker(self, self._worker, resources):
            self._on_all_done(0, len(exes), action_label)
            InfoBar.warning(
                title='作業進行中',
                content='目標檔案或 Steamless 正由另一個作業使用',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3500,
            )

    def _on_worker_all_done(self, success: int, fail: int) -> None:
        worker = self.sender()
        action_label = (
            str(worker.property('action_label') or '作業')
            if isinstance(worker, QObject)
            else '作業'
        )
        self._on_all_done(success, fail, action_label)

    def _append_log(self, text: str):
        self.log_view.appendPlainText(text)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_all_done(self, success: int, fail: int, action_label: str):
        self.deploy_btn.setEnabled(True)
        self.remove_btn.setEnabled(True)
        self.add_exe_btn.setEnabled(True)
        self.remove_target_btn.setEnabled(True)
        self.clear_target_btn.setEnabled(True)
        self._append_log(
            f'\n===== {action_label}完成：成功 {success}、失敗 {fail} ====='
        )
        self._refresh_target_states()
        if fail == 0 and success > 0:
            InfoBar.success(
                title=f'{action_label}完成',
                content=f'共 {success} 個目錄',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )
        elif success > 0:
            InfoBar.warning(
                title='部分完成',
                content=f'成功 {success}、失敗 {fail}',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        else:
            InfoBar.error(
                title='全部失敗',
                content=f'失敗 {fail}',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )

    def _on_open_selected_dir(self):
        items = self.target_list.selectedItems()
        if not items:
            InfoBar.info(
                title='未選取',
                content='請先在清單中點選要開啟的項目',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=2500,
            )
            return
        executable = Path(items[0].data(Qt.ItemDataRole.UserRole))
        if sys.platform == 'win32':
            import subprocess

            startup_info = subprocess.STARTUPINFO()
            startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup_info.wShowWindow = subprocess.SW_HIDE
            try:
                subprocess.Popen(
                    ['explorer', '/select,', str(executable)],
                    startupinfo=startup_info,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except Exception:
                pass


__all__ = ['KrkrzPage']
