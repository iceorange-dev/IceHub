"""OpenSteamTool management page."""

from __future__ import annotations

from functools import partial
from typing import Dict, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
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
    TitleLabel,
)

from app.pages.common import error_text as _error_text, resolve_backend_provider
from app.services.tasks import start_registered_worker as _start_registered_worker
from app.tasking import AsyncWorker
from backend.ost_backend import (
    OSTBackend,
    OST_DEFAULT_REPO,
    detect_ost,
    detect_steam_path,
    is_steam_running,
    load_config,
    version_newer,
)
from backend.runtime import BackendRuntime, resolve_runtime

class OSTPage(ScrollArea):
    """OpenSteamTool installation, update, and removal management."""

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
        self.setObjectName('ost')
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

        self.main_layout.addWidget(TitleLabel('OpenSteamTool 管理', container))
        subtitle = CaptionLabel(
            '管理 OpenSteamTool 三件套的安裝、更新與解除安裝',
            container,
        )
        subtitle.setTextColor('#666', '#aaa')
        self.main_layout.addWidget(subtitle)

        self.card = GroupHeaderCardWidget(container)
        self.card.setTitle('OpenSteamTool')
        self.card.setBorderRadius(8)

        status_widget = QWidget()
        status_layout = QVBoxLayout(status_widget)
        status_layout.setContentsMargins(0, 0, 0, 0)
        self.status_label = BodyLabel('正在檢測…')
        status_layout.addWidget(self.status_label)
        status_button_row = QHBoxLayout()
        self.refresh_btn = PushButton('重新檢測')
        self.refresh_btn.setIcon(FluentIcon.SYNC)
        self.refresh_btn.clicked.connect(self.refresh_status)
        self.open_steam_btn = PushButton('開啟 Steam 目錄')
        self.open_steam_btn.setIcon(FluentIcon.FOLDER)
        self.open_steam_btn.clicked.connect(self._open_steam_dir)
        self.github_link = HyperlinkButton(
            f'https://github.com/{OST_DEFAULT_REPO}',
            'GitHub',
        )
        status_button_row.addWidget(self.refresh_btn)
        status_button_row.addWidget(self.open_steam_btn)
        status_button_row.addWidget(self.github_link)
        status_button_row.addStretch(1)
        status_layout.addLayout(status_button_row)
        self.card.addGroup(
            FluentIcon.APPLICATION,
            '檔案狀態',
            'OpenSteamTool.dll / dwmapi.dll / xinput1_4.dll 必須在 Steam 根目錄',
            status_widget,
        )

        install_widget = QWidget()
        install_layout = QVBoxLayout(install_widget)
        install_layout.setContentsMargins(0, 0, 0, 0)
        version_row = QHBoxLayout()
        self.local_ver_label = BodyLabel('本地版本: 未知')
        self.latest_ver_label = BodyLabel('最新版本: 檢查中…')
        version_row.addWidget(self.local_ver_label)
        version_row.addSpacing(20)
        version_row.addWidget(self.latest_ver_label)
        version_row.addStretch(1)
        install_layout.addLayout(version_row)

        self.progress = ProgressBar(install_widget)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.hide()
        install_layout.addWidget(self.progress)
        self.progress_label = CaptionLabel('', install_widget)
        self.progress_label.setTextColor('#888', '#aaa')
        self.progress_label.hide()
        install_layout.addWidget(self.progress_label)

        install_button_row = QHBoxLayout()
        self.check_update_btn = PushButton('檢查更新')
        self.check_update_btn.setIcon(FluentIcon.SYNC)
        self.check_update_btn.clicked.connect(self.check_update)
        install_button_row.addWidget(self.check_update_btn)
        self.install_btn = PrimaryPushButton('安裝 / 更新')
        self.install_btn.setIcon(FluentIcon.DOWNLOAD)
        self.install_btn.clicked.connect(self.install_ost)
        install_button_row.addWidget(self.install_btn)
        install_button_row.addStretch(1)
        install_layout.addLayout(install_button_row)
        self.card.addGroup(
            FluentIcon.DOWNLOAD,
            '安裝 / 更新',
            '從 GitHub 拉取最新 release zip 並部署到 Steam 根目錄',
            install_widget,
        )

        uninstall_widget = QWidget()
        uninstall_layout = QVBoxLayout(uninstall_widget)
        uninstall_layout.setContentsMargins(0, 0, 0, 0)
        self.keep_lua_check = CheckBox(
            '保留已入庫遊戲（config/lua/*.lua）'
        )
        self.keep_lua_check.setChecked(True)
        uninstall_layout.addWidget(self.keep_lua_check)
        uninstall_hint = CaptionLabel(
            '只還原／移除 IceHub 管理的三個 OST DLL；'
            '使用者設定、執行日誌與舊版備份會保留'
        )
        uninstall_hint.setTextColor('#888', '#aaa')
        uninstall_hint.setWordWrap(True)
        uninstall_layout.addWidget(uninstall_hint)
        uninstall_button_row = QHBoxLayout()
        self.uninstall_btn = PushButton('解除安裝 OST')
        self.uninstall_btn.setIcon(FluentIcon.DELETE)
        self.uninstall_btn.clicked.connect(self.uninstall_ost)
        uninstall_button_row.addWidget(self.uninstall_btn)
        uninstall_button_row.addStretch(1)
        uninstall_layout.addLayout(uninstall_button_row)
        self.card.addGroup(
            FluentIcon.DELETE,
            '解除安裝',
            '從 Steam 目錄移除 OST，並可選擇是否保留已入庫遊戲',
            uninstall_widget,
        )

        self.main_layout.addWidget(self.card)
        self.main_layout.addStretch(1)
        self._latest_release: Optional[Dict] = None
        self._check_worker: Optional[AsyncWorker] = None
        self._install_worker: Optional[AsyncWorker] = None
        self._uninstall_worker: Optional[AsyncWorker] = None

        QTimer.singleShot(0, self.refresh_status)
        QTimer.singleShot(500, self.check_update)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_status()
        self._update_local_version()

    def refresh_status(self):
        config = load_config(runtime=self.runtime)
        steam = detect_steam_path(config.get('Custom_Steam_Path', ''))
        if not steam or not steam.exists():
            self.status_label.setText(
                '⚠ 未找到 Steam 目錄，請到「設定」頁設定路徑'
            )
            self._update_local_version()
            return
        status = detect_ost(steam, runtime=self.runtime)
        lines = [f'Steam 路徑: {steam}']
        all_ok = True
        for filename, exists in status.items():
            mark = '✓' if exists else '✗'
            lines.append(f'  {mark} {filename}')
            if not exists:
                all_ok = False
        lines.append('')
        if all_ok:
            lines.append('✓ OST 已正確部署')
        else:
            lines.append('請點選下方「安裝 / 更新」按鈕自動部署 OST。')
        self.status_label.setText('\n'.join(lines))
        self._update_local_version()

    def _update_local_version(self):
        config = load_config(runtime=self.runtime)
        local = config.get('installed_ost_version', '') or '未知'
        self.local_ver_label.setText(f'本地版本: {local}')

    def _open_steam_dir(self):
        config = load_config(runtime=self.runtime)
        steam = detect_steam_path(config.get('Custom_Steam_Path', ''))
        if steam and steam.exists():
            import subprocess

            subprocess.Popen(['explorer', str(steam)])

    def check_update(self):
        if self._check_worker and self._check_worker.isRunning():
            return
        self._update_local_version()
        self.latest_ver_label.setText('最新版本: 檢查中…')
        self.check_update_btn.setEnabled(False)

        async def fetch():
            async with self.backend_provider() as backend:
                return await backend.fetch_ost_latest()

        self._check_worker = AsyncWorker(fetch)
        self._check_worker.result.connect(self._on_update_checked)
        self._check_worker.error.connect(self._on_update_check_failed)
        if not _start_registered_worker(
            self,
            self._check_worker,
            'ost-release-check',
        ):
            self.check_update_btn.setEnabled(True)

    def _on_update_checked(self, release):
        self.check_update_btn.setEnabled(True)
        if not release:
            self.latest_ver_label.setText('最新版本: 取得失敗')
            return
        self._latest_release = release
        tag = release.get('tag_name', '')
        self.latest_ver_label.setText(f'最新版本: {tag}')
        config = load_config(runtime=self.runtime)
        local = config.get('installed_ost_version', '')
        if local and tag and not version_newer(tag, local):
            self.install_btn.setText('重新安裝')
        else:
            self.install_btn.setText('安裝 / 更新')

    def _on_update_check_failed(self, error: object):
        self.check_update_btn.setEnabled(True)
        self.latest_ver_label.setText(
            f'最新版本: 失敗 ({_error_text(error)[:40]})'
        )

    def install_ost(self):
        if self._install_worker and self._install_worker.isRunning():
            return

        original_text = self.install_btn.text()
        self.install_btn.setEnabled(False)
        self.install_btn.setText('檢查中…')

        def bail():
            self.install_btn.setEnabled(True)
            self.install_btn.setText(original_text)

        config = load_config(runtime=self.runtime)
        steam = detect_steam_path(config.get('Custom_Steam_Path', ''))
        if not steam or not steam.exists():
            bail()
            InfoBar.error(
                title='Steam 未找到',
                content='請先到「設定」頁設定 Steam 路徑',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )
            return

        if is_steam_running():
            bail()
            message = MessageBox(
                'Steam 正在執行',
                '安裝 OST 需要覆蓋 dll 檔案，必須先完全關閉 Steam。'
                '\n\n請關閉 Steam 後重試。',
                self.window(),
            )
            message.exec()
            return

        confirm = MessageBox(
            '確認安裝 OST',
            f'將下載 OST 最新 release 並部署到:\n{steam}\n\n'
            'IceHub 會記錄檔案 ownership，並安全保存首次安裝前的原始 DLL。'
            '\n繼續？',
            self.window(),
        )
        if not confirm.exec():
            bail()
            return

        self.install_btn.setText('安裝中…')
        self.check_update_btn.setEnabled(False)
        self.uninstall_btn.setEnabled(False)
        self.progress.show()
        self.progress.setValue(0)
        self.progress_label.show()
        self.progress_label.setText('準備中…')

        async def install(report):
            def progress_callback(stage: str, current: int, total: int):
                if stage == 'fetch':
                    text = '取得 release 資訊…'
                    value = 5
                elif stage == 'download':
                    percentage = (
                        int(current * 90 / max(total, 1)) if total else 50
                    )
                    text = (
                        f'下載中 {current // 1024} / '
                        f'{(total or current) // 1024} KB'
                    )
                    value = 5 + percentage
                elif stage == 'install':
                    text = '部署 dll 到 Steam 目錄…'
                    value = 95 if current == 0 else 100
                else:
                    text = stage
                    value = 50
                report((text, value))

            async with self.backend_provider() as backend:
                ok, error = backend.initialize()
                if not ok:
                    return False, error
                return await backend.install_or_update_ost(progress_callback)

        self._install_worker = AsyncWorker(install)
        self._install_worker.progress.connect(self._on_install_progress)
        self._install_worker.result.connect(self._on_install_finished)
        self._install_worker.error.connect(self._on_install_worker_error)
        if not _start_registered_worker(
            self,
            self._install_worker,
            'ost-files',
        ):
            self._on_install_finished(
                (False, '另一個 OST 安裝或移除作業正在執行')
            )

    def _on_install_progress(self, value: object) -> None:
        if isinstance(value, tuple) and len(value) == 2:
            self._update_progress(str(value[0]), int(value[1]))

    def _on_install_worker_error(self, error: object) -> None:
        self._on_install_finished((False, _error_text(error)))

    def _update_progress(self, text: str, value: int):
        try:
            self.progress.setValue(max(0, min(100, value)))
            self.progress_label.setText(text)
        except Exception:
            pass

    def _on_install_finished(self, result):
        self.install_btn.setEnabled(True)
        self.check_update_btn.setEnabled(True)
        self.uninstall_btn.setEnabled(True)
        config = load_config(runtime=self.runtime)
        local = config.get('installed_ost_version', '')
        latest_tag = (self._latest_release or {}).get('tag_name', '')
        if local and latest_tag and not version_newer(latest_tag, local):
            self.install_btn.setText('重新安裝')
        else:
            self.install_btn.setText('安裝 / 更新')
        self.uninstall_btn.setText('解除安裝 OST')

        ok, message = result if isinstance(result, tuple) else (False, str(result))
        self.progress.setValue(100 if ok else 0)
        self.progress_label.setText(message)
        if ok:
            InfoBar.success(
                title='安裝完成',
                content=message,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        else:
            InfoBar.error(
                title='安裝失敗',
                content=message,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )
        self.refresh_status()
        QTimer.singleShot(3000, self._hide_progress)

    def _hide_progress(self) -> None:
        self.progress.hide()
        self.progress_label.hide()

    def uninstall_ost(self):
        if self._uninstall_worker and self._uninstall_worker.isRunning():
            return

        original_text = self.uninstall_btn.text()
        self.uninstall_btn.setEnabled(False)
        self.uninstall_btn.setText('檢查中…')

        def bail():
            self.uninstall_btn.setEnabled(True)
            self.uninstall_btn.setText(original_text)

        config = load_config(runtime=self.runtime)
        steam = detect_steam_path(config.get('Custom_Steam_Path', ''))
        if not steam or not steam.exists():
            bail()
            InfoBar.error(
                title='Steam 未找到',
                content='請先到「設定」頁設定 Steam 路徑',
                parent=self,
                position=InfoBarPosition.TOP,
                duration=4000,
            )
            return

        if is_steam_running():
            bail()
            message = MessageBox(
                'Steam 正在執行',
                '解除安裝 OST 需要刪除 dll 檔案，必須先完全關閉 Steam。'
                '\n\n請關閉 Steam 後重試。',
                self.window(),
            )
            message.exec()
            return

        keep_games = self.keep_lua_check.isChecked()
        keep_message = (
            '保留 config/lua/ 中已入庫的遊戲'
            if keep_games
            else '一併移除 IceHub 管理的已入庫遊戲（未管理的 Lua 會保留）'
        )
        confirm = MessageBox(
            '確認解除安裝 OST',
            f'將從 {steam} 移除：\n'
            '  • OpenSteamTool.dll\n'
            '  • dwmapi.dll\n'
            '  • xinput1_4.dll\n'
            '若安裝前已有同名 DLL，會還原已驗證的原始版本。\n'
            'opensteamtool.toml、日誌與舊版 .ost_backup 會保留。\n\n'
            f'已入庫遊戲：{keep_message}\n\n'
            '繼續？',
            self.window(),
        )
        if not confirm.exec():
            bail()
            return

        self.uninstall_btn.setText('解除安裝中…')
        self.install_btn.setEnabled(False)
        self.check_update_btn.setEnabled(False)

        async def uninstall():
            backend = self.backend_provider()
            initialized, error = backend.initialize()
            if not initialized:
                return False, error, []
            ok, message, removed = backend.uninstall_ost(
                keep_installed_games=keep_games
            )
            return ok, message, removed

        self._uninstall_worker = AsyncWorker(uninstall)
        self._uninstall_worker.result.connect(self._on_uninstall_finished)
        self._uninstall_worker.error.connect(self._on_uninstall_worker_error)
        if not _start_registered_worker(
            self,
            self._uninstall_worker,
            'ost-files',
        ):
            self._on_uninstall_finished(
                (
                    False,
                    '另一個 OST 安裝或移除作業正在執行',
                    [],
                )
            )

    def _on_uninstall_worker_error(self, error: object) -> None:
        self._on_uninstall_finished((False, _error_text(error), []))

    def _on_uninstall_finished(self, result):
        self.uninstall_btn.setEnabled(True)
        self.install_btn.setEnabled(True)
        self.check_update_btn.setEnabled(True)
        self.uninstall_btn.setText('解除安裝 OST')
        if isinstance(result, tuple) and len(result) == 3:
            ok, message, removed = result
        elif isinstance(result, tuple):
            ok, message = result
            removed = []
        else:
            ok = False
            message = str(result)
            removed = []

        if ok:
            removed_text = '、'.join(removed) if removed else '(無項目)'
            InfoBar.success(
                title='OST 已解除安裝',
                content=f'{message}：{removed_text}'[:300],
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )
        else:
            InfoBar.error(
                title='解除安裝失敗',
                content=message,
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )
        self.refresh_status()
        self._update_local_version()


__all__ = ['OSTPage']
