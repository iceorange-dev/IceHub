"""Top-level IceHub window and page composition."""

from __future__ import annotations

from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QMessageBox
from qfluentwidgets import FluentIcon, MSFluentWindow, NavigationItemPosition

from app.pages.home import HomePage
from app.pages.installed import InstalledPage
from app.pages.krkrz import KrkrzPage
from app.pages.ost import OSTPage
from app.pages.settings import SettingsPage
from app.pages.steamless import SteamlessPage
from app.services.context import AppServices
from app.services.image_loader import ImageLoader
from app.version import VERSION
from backend.resources import resource_path


class MainWindow(MSFluentWindow):
    """Own the service graph and compose the six navigation pages."""

    def __init__(self, *, app_services: AppServices | None = None) -> None:
        super().__init__()
        self.app_services = app_services or AppServices(self)
        # Compatibility aliases used by existing extensions and smoke tests.
        self.worker_registry = self.app_services.worker_registry
        self.operation_coordinator = self.app_services.operation_coordinator
        self.setWindowTitle(f'IceHub v{VERSION}')
        self.resize(1100, 720)

        icon_path = resource_path(
            'assets/icon.ico',
            runtime=self.app_services.runtime,
        )
        if icon_path.exists():
            icon = QIcon(str(icon_path))
            self.setWindowIcon(icon)
            title_bar = getattr(self, 'titleBar', None)
            if title_bar is not None:
                try:
                    title_bar.setIcon(icon)
                except (AttributeError, RuntimeError, TypeError):
                    pass

        self.home_page = HomePage(
            self.app_services.backend_provider,
            self,
            runtime=self.app_services.runtime,
        )
        self.installed_page = InstalledPage(
            self,
            backend_provider=self.app_services.backend_provider,
            runtime=self.app_services.runtime,
        )
        self.ost_page = OSTPage(
            self,
            backend_provider=self.app_services.backend_provider,
            runtime=self.app_services.runtime,
        )
        self.steamless_page = SteamlessPage(
            self,
            backend_provider=self.app_services.backend_provider,
            runtime=self.app_services.runtime,
        )
        self.krkrz_page = KrkrzPage(
            self,
            runtime=self.app_services.runtime,
        )
        self.settings_page = SettingsPage(
            self,
            runtime=self.app_services.runtime,
        )

        self.addSubInterface(self.home_page, FluentIcon.HOME, '首頁')
        self.addSubInterface(self.installed_page, FluentIcon.GAME, '已入庫')
        self.addSubInterface(self.ost_page, FluentIcon.APPLICATION, 'OST 管理')
        self.addSubInterface(
            self.steamless_page,
            FluentIcon.BROOM,
            'Steamless 脫殼',
        )
        self.addSubInterface(
            self.krkrz_page,
            FluentIcon.DEVELOPER_TOOLS,
            'KRKRZ 補丁',
        )
        self.addSubInterface(
            self.settings_page,
            FluentIcon.SETTING,
            '設定',
            position=NavigationItemPosition.BOTTOM,
        )

        self.settings_page.saved.connect(self.home_page.refresh_status)
        self.settings_page.saved.connect(self.ost_page.refresh_status)
        self.settings_page.saved.connect(self.home_page.reload_settings)
        self.settings_page.saved.connect(self.installed_page.reload_settings)

    def closeEvent(self, event) -> None:
        """Cooperatively finish background cleanup before closing widgets."""

        if self.worker_registry.running_workers():
            self.worker_registry.cancel_all()
            if not self.worker_registry.wait_for_all(10000):
                event.ignore()
                QMessageBox.information(
                    self,
                    '作業仍在結束中',
                    '已要求取消背景作業，但清理尚未完成。請稍候再關閉 IceHub。',
                )
                return
        if ImageLoader._instance is not None:
            ImageLoader._instance.shutdown()
        super().closeEvent(event)


__all__ = ['MainWindow']
