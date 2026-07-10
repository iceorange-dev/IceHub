"""Game result card used by the Steam catalog page."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import BodyLabel, CaptionLabel, CardWidget, PrimaryPushButton

from app.services.image_loader import ImageLoader
from backend.cache import get_cached_header


class GameCard(CardWidget):
    """Display one Steam result and emit a guarded install request."""

    install_clicked = pyqtSignal(str, str)

    def __init__(
        self,
        game: Mapping[str, Any],
        parent: QWidget | None = None,
        *,
        image_loader: ImageLoader | None = None,
        header_provider: Callable[[str], str] = get_cached_header,
    ) -> None:
        super().__init__(parent)
        self.game = dict(game)
        self.appid = str(game.get('appid', ''))
        self.name = str(game.get('name') or f'AppID {self.appid}')
        self.header = str(game.get('header_image', ''))
        self._installed = False
        self._busy = False
        self._image_loader = image_loader or ImageLoader.instance()

        self.setFixedSize(280, 200)
        self.setBorderRadius(10)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.cover = QLabel(self)
        self.cover.setFixedSize(280, 130)
        self.cover.setScaledContents(True)
        self.cover.setStyleSheet(
            'background: #2a2a2e; border-top-left-radius: 10px; '
            'border-top-right-radius: 10px;'
        )
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setText('載入中…')
        layout.addWidget(self.cover)

        cached = header_provider(self.appid)
        primary = cached or self.header or (
            'https://cdn.cloudflare.steamstatic.com/steam/apps/'
            f'{self.appid}/header.jpg'
        )
        fallback = self.header if cached and self.header != cached else None
        self._image_loader.load(
            primary,
            self._on_cover_loaded,
            appid=self.appid,
            fallback_url=fallback,
        )

        info = QWidget(self)
        info.setFixedHeight(70)
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(12, 8, 12, 8)
        info_layout.setSpacing(4)

        self.name_label = BodyLabel(self.name, info)
        self.name_label.setWordWrap(False)
        name_font = self.name_label.font()
        name_font.setWeight(QFont.Weight.DemiBold)
        self.name_label.setFont(name_font)
        info_layout.addWidget(self.name_label)

        bottom = QHBoxLayout()
        appid_label = CaptionLabel(f'AppID: {self.appid}', info)
        appid_label.setTextColor('#888', '#aaa')
        bottom.addWidget(appid_label)
        bottom.addStretch(1)

        self.install_btn = PrimaryPushButton('入庫', info)
        self.install_btn.setFixedHeight(28)
        self.install_btn.clicked.connect(self._on_install)
        bottom.addWidget(self.install_btn)
        info_layout.addLayout(bottom)
        layout.addWidget(info)

    def _on_cover_loaded(self, pixmap: QPixmap) -> None:
        try:
            self.cover.setText('')
            self.cover.setPixmap(
                pixmap.scaled(
                    self.cover.size(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        except (RuntimeError, TypeError):
            # The asynchronous image may arrive after the card was destroyed.
            pass

    def _on_install(self) -> None:
        if not self._installed and not self._busy:
            self.install_clicked.emit(self.appid, self.name)

    def _refresh_button(self) -> None:
        if self._installed:
            self.install_btn.setText('已入庫')
            self.install_btn.setEnabled(False)
        elif self._busy:
            self.install_btn.setText('入庫中…')
            self.install_btn.setEnabled(False)
        else:
            self.install_btn.setText('入庫')
            self.install_btn.setEnabled(True)

    def set_installed(self, installed: bool) -> None:
        self._installed = installed
        self._refresh_button()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_button()


__all__ = ['GameCard']
