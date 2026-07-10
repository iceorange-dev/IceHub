from __future__ import annotations

from typing import Any

from PyQt6.QtGui import QFont, QPalette, QPixmap
from qfluentwidgets import Theme, setTheme

from app.widgets.game_card import GameCard


class FakeImageLoader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []

    def load(
        self,
        url: str,
        callback,
        *,
        appid: str | None = None,
        fallback_url: str | None = None,
    ) -> None:
        self.calls.append((url, appid, fallback_url))
        callback(QPixmap())


def test_game_card_emits_install_once_only_while_available(
    qtbot: Any,
) -> None:
    loader = FakeImageLoader()
    card = GameCard(
        {
            'appid': '480',
            'name': 'Spacewar',
            'header_image': 'https://cdn.test/480.jpg',
        },
        image_loader=loader,  # type: ignore[arg-type]
        header_provider=lambda _appid: '',
    )
    qtbot.addWidget(card)
    emitted: list[tuple[str, str]] = []
    card.install_clicked.connect(lambda appid, name: emitted.append((appid, name)))

    card.install_btn.click()
    card.set_busy(True)
    card.install_btn.click()
    card.set_busy(False)
    card.set_installed(True)
    card.install_btn.click()

    assert emitted == [('480', 'Spacewar')]
    assert loader.calls == [('https://cdn.test/480.jpg', '480', None)]
    assert card.install_btn.text() == '已入庫'
    assert not card.install_btn.isEnabled()


def test_game_card_uses_appid_fallback_header(qtbot: Any) -> None:
    loader = FakeImageLoader()
    card = GameCard(
        {'appid': '570', 'name': ''},
        image_loader=loader,  # type: ignore[arg-type]
        header_provider=lambda _appid: '',
    )
    qtbot.addWidget(card)

    assert card.name == 'AppID 570'
    assert loader.calls[0][0].endswith('/570/header.jpg')


def test_game_card_title_keeps_dark_theme_color_when_made_bold(
    qtbot: Any,
) -> None:
    setTheme(Theme.DARK)
    try:
        card = GameCard(
            {'appid': '480', 'name': 'Spacewar'},
            image_loader=FakeImageLoader(),  # type: ignore[arg-type]
            header_provider=lambda _appid: '',
        )
        qtbot.addWidget(card)
        qtbot.wait(10)

        title = card.name_label
        style = title.styleSheet().strip().lower()
        color = title.palette().color(QPalette.ColorRole.WindowText)
        assert title.font().weight() == QFont.Weight.DemiBold
        assert 'font-weight' not in style
        assert 'color' in style
        assert color.lightness() > 127
    finally:
        setTheme(Theme.AUTO)
