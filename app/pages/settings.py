"""Application settings and about page."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QFileDialog, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    FluentIcon,
    GroupHeaderCardWidget,
    HyperlinkButton,
    InfoBar,
    InfoBarPosition,
    LineEdit,
    PrimaryPushButton,
    ScrollArea,
    TitleLabel,
    TransparentToolButton,
)

from app.version import VERSION
from backend.ost_backend import load_config, update_config_fields
from backend.runtime import BackendRuntime, resolve_runtime


class SettingsPage(ScrollArea):
    saved = pyqtSignal()

    def __init__(
        self,
        parent=None,
        *,
        runtime: BackendRuntime | None = None,
    ):
        super().__init__(parent)
        self.runtime = resolve_runtime(runtime)
        self.setObjectName('settings')
        self.setWidgetResizable(True)
        self.setStyleSheet('QScrollArea { background: transparent; border: none; }')

        container = QWidget()
        container.setStyleSheet('background: transparent;')
        self.setWidget(container)
        self.main_layout = QVBoxLayout(container)
        self.main_layout.setContentsMargins(30, 24, 30, 24)
        self.main_layout.setSpacing(16)

        self.main_layout.addWidget(TitleLabel('設定', container))

        # ---- 基礎設定卡 ----
        self.basic_card = GroupHeaderCardWidget(container)
        self.basic_card.setTitle('基本設定')
        self.basic_card.setBorderRadius(8)

        # Steam 路徑
        steam_widget = QWidget()
        steam_layout = QHBoxLayout(steam_widget)
        steam_layout.setContentsMargins(0, 0, 0, 0)
        self.steam_path_edit = LineEdit()
        self.steam_path_edit.setPlaceholderText('留空自動檢測')
        self.steam_path_edit.setFixedWidth(320)
        steam_layout.addWidget(self.steam_path_edit)
        self.browse_btn = TransparentToolButton(FluentIcon.FOLDER)
        self.browse_btn.clicked.connect(self._browse_steam)
        steam_layout.addWidget(self.browse_btn)
        self.basic_card.addGroup(FluentIcon.FOLDER, 'Steam 路徑', '指向 Steam 安裝目錄', steam_widget)

        # Ryuu key
        ryuu_widget = QWidget()
        ryuu_layout = QHBoxLayout(ryuu_widget)
        ryuu_layout.setContentsMargins(0, 0, 0, 0)
        self.ryuu_key_edit = LineEdit()
        self.ryuu_key_edit.setPlaceholderText('Ryuu Generator auth_key')
        self.ryuu_key_edit.setFixedWidth(320)
        self.ryuu_key_edit.setEchoMode(LineEdit.EchoMode.Password)
        ryuu_layout.addWidget(self.ryuu_key_edit)
        self.ryuu_link = HyperlinkButton('https://generator.ryuu.lol/api', '取得 Key')
        self.ryuu_link.setFixedWidth(90)
        ryuu_layout.addWidget(self.ryuu_link)
        self.basic_card.addGroup(FluentIcon.VPN, 'Ryuu Auth Key', '主要清單來源', ryuu_widget)

        # 來源
        self.source_combo = ComboBox()
        self.source_combo.addItems(['自動 (Ryuu → Sudama)', '僅 Ryuu', '僅 Sudama'])
        self.source_combo.setFixedWidth(220)
        self.basic_card.addGroup(FluentIcon.SYNC, '預設來源', '入庫時優先使用的清單源', self.source_combo)

        self.main_layout.addWidget(self.basic_card)

        # ---- 關於 ----
        self.about_card = GroupHeaderCardWidget(container)
        self.about_card.setTitle('關於')
        self.about_card.setBorderRadius(8)

        about_widget = QWidget()
        about_layout = QVBoxLayout(about_widget)
        about_layout.setContentsMargins(0, 0, 0, 0)
        about_layout.setSpacing(8)

        ver = BodyLabel(f'IceHub v{VERSION}')
        version_font = ver.font()
        version_font.setWeight(QFont.Weight.DemiBold)
        ver.setFont(version_font)
        about_layout.addWidget(ver)

        author = BodyLabel('由 IceOrange 製作')
        about_layout.addWidget(author)

        free_notice = CaptionLabel(
            '本軟體為免費軟體，僅供個人學習與研究用途；'
            '禁止任何形式之販售、收費散布或商業利用。'
        )
        free_notice.setTextColor('#888', '#aaa')
        free_notice.setWordWrap(True)
        about_layout.addWidget(free_notice)

        inspired_row = QHBoxLayout()
        inspired_row.setContentsMargins(0, 0, 0, 0)
        inspired_lbl = BodyLabel('Inspired by FluentInstall')
        inspired_row.addWidget(inspired_lbl)
        self.fi_link = HyperlinkButton(
            'https://github.com/zhouchentao666/Fluent-Install',
            'GitHub',
        )
        inspired_row.addWidget(self.fi_link)
        inspired_row.addStretch(1)
        about_layout.addLayout(inspired_row)

        self.about_card.addGroup(
            FluentIcon.INFO, 'IceHub',
            'OST 入庫 / Steamless 脫殼 / KRKRZ 補丁',
            about_widget,
        )
        self.main_layout.addWidget(self.about_card)

        # ---- 儲存按鈕 ----
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.save_btn = PrimaryPushButton('儲存設定')
        self.save_btn.setIcon(FluentIcon.SAVE)
        self.save_btn.clicked.connect(self._save)
        save_row.addWidget(self.save_btn)
        self.main_layout.addLayout(save_row)

        self.main_layout.addStretch(1)

        self._load()

    def _load(self):
        cfg = load_config(runtime=self.runtime)
        self.steam_path_edit.setText(cfg.get('Custom_Steam_Path', ''))
        self.ryuu_key_edit.setText(cfg.get('Ryuu_Auth_Key', ''))
        idx_map = {'auto': 0, 'ryuu': 1, 'sudama': 2}
        self.source_combo.setCurrentIndex(idx_map.get(cfg.get('default_source', 'auto'), 0))

    def _save(self):
        src_map = {0: 'auto', 1: 'ryuu', 2: 'sudama'}
        try:
            update_config_fields(
                {
                    'Custom_Steam_Path': self.steam_path_edit.text().strip(),
                    'Ryuu_Auth_Key': self.ryuu_key_edit.text().strip(),
                    'default_source': src_map.get(
                        self.source_combo.currentIndex(),
                        'auto',
                    ),
                },
                runtime=self.runtime,
            )
        except Exception as error:
            InfoBar.error(
                title='儲存失敗', content=str(error), parent=self,
                position=InfoBarPosition.TOP, duration=5000,
            )
            return
        InfoBar.success(
            title='已儲存',
            content='設定已寫入 config.json',
            parent=self,
            position=InfoBarPosition.TOP,
            duration=2000,
        )
        self.saved.emit()

    def _browse_steam(self):
        current = self.steam_path_edit.text().strip()
        initial = current or 'C:/Program Files (x86)/Steam'
        folder = QFileDialog.getExistingDirectory(self, '選擇 Steam 安裝目錄', initial)
        if folder:
            self.steam_path_edit.setText(folder)


__all__ = ['SettingsPage']
