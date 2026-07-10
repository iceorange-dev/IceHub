"""Qt application bootstrap and single-instance lifecycle."""

from __future__ import annotations

from pathlib import Path
import sys

from PyQt6.QtCore import QLockFile, Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QMessageBox
from qfluentwidgets import Theme, setTheme, setThemeColor

from app.shell.main_window import MainWindow
from backend.ost_backend import APP_ROOT, load_config, resource_path, setup_logger


def _set_windows_app_id() -> None:
    if sys.platform != 'win32':
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'com.icehub.app'
        )
    except (AttributeError, OSError):
        pass


def _apply_theme(config: dict) -> None:
    mode = config.get('theme_mode', 'auto')
    setTheme(
        {
            'light': Theme.LIGHT,
            'dark': Theme.DARK,
        }.get(mode, Theme.AUTO)
    )
    setThemeColor(config.get('theme_color', '#ff8c00'))


def main() -> int:
    """Create the QApplication, enforce one instance, and run the window."""

    setup_logger()
    _set_windows_app_id()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    application = QApplication(sys.argv)

    instance_dir = Path(APP_ROOT)
    instance_dir.mkdir(parents=True, exist_ok=True)
    instance_lock = QLockFile(str(instance_dir / 'icehub-instance.lock'))
    instance_lock.setStaleLockTime(30000)
    if not instance_lock.tryLock(100):
        QMessageBox.information(
            None,
            'IceHub 已在執行',
            '另一個 IceHub 視窗已經開啟。',
        )
        return 0

    try:
        config = load_config()
        setup_logger(debug=bool(config.get('debug_mode', False)))

        icon_path = resource_path('assets/icon.ico')
        if icon_path.exists():
            application.setWindowIcon(QIcon(str(icon_path)))

        _apply_theme(config)
        window = MainWindow()
        window.show()
        return int(application.exec())
    finally:
        instance_lock.unlock()


__all__ = ['main']
