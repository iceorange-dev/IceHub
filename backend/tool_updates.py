"""High-level OST and Steamless release update workflows.

The installer modules own local deployment and rollback.  This service owns
the remote release workflow around them: selecting a trusted repository,
downloading a release asset, invoking the installer, and updating the display
version cache.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

import httpx

from .config import update_config_fields
from .constants import OST_DEFAULT_REPO, STEAMLESS_DEFAULT_REPO
from .http import download_file
from .installers.ost import install_ost_from_zip, uninstall_ost
from .installers.steamless import (
    install_steamless_from_zip,
    pick_steamless_zip,
    uninstall_steamless,
)
from .platform.steam import is_steam_running
from .releases import fetch_latest_release, pick_release_zip
from .runtime import BackendRuntime


ProgressCallback = Callable[[str, int, int], None]
ClientScope = Callable[[], AbstractAsyncContextManager[httpx.AsyncClient]]


class ToolUpdateService:
    """Coordinate remote releases with the transactional local installers."""

    def __init__(
        self,
        runtime: BackendRuntime,
        config: dict[str, Any],
        client_scope: ClientScope,
        *,
        steam_running: Callable[[], bool] = is_steam_running,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self._client_scope = client_scope
        self._steam_running = steam_running
        self.log = runtime.log

    @staticmethod
    def _progress(
        callback: ProgressCallback | None,
        stage: str,
        current: int = 0,
        total: int = 0,
    ) -> None:
        if callback is None:
            return
        try:
            callback(stage, current, total)
        except Exception:
            # UI progress reporting must never abort a deployment.
            pass

    async def _download_asset(
        self,
        asset: dict[str, Any],
        progress_cb: ProgressCallback | None,
    ) -> Path | None:
        self.runtime.temp_dir.mkdir(parents=True, exist_ok=True)
        asset_name = str(asset.get('name', ''))
        if Path(asset_name).name != asset_name or not asset_name.lower().endswith('.zip'):
            return None

        zip_path = self.runtime.temp_dir / asset_name
        expected_size = int(asset.get('size', 0) or 0)
        self._progress(progress_cb, 'download', 0, expected_size)

        def report(current: int, total: int) -> None:
            self._progress(progress_cb, 'download', current, total)

        async with self._client_scope() as client:
            downloaded = await download_file(
                client,
                str(asset['browser_download_url']),
                zip_path,
                report,
                expected_size=expected_size,
                expected_digest=str(asset.get('digest', '') or ''),
            )
        return zip_path if downloaded else None

    async def fetch_ost_latest(self) -> dict[str, Any] | None:
        repo = str(self.config.get('OST_Repo', OST_DEFAULT_REPO))
        async with self._client_scope() as client:
            return await fetch_latest_release(
                client,
                repo,
                allowed_repos={OST_DEFAULT_REPO},
            )

    async def install_or_update_ost(
        self,
        steam_path: Path | None,
        progress_cb: ProgressCallback | None = None,
    ) -> tuple[bool, str]:
        if not steam_path or not steam_path.exists():
            return False, '未配置 Steam 路徑，請先在設定中指定'
        if self._steam_running():
            return False, 'Steam 仍在執行，請先完全關閉 Steam 後重試'

        self._progress(progress_cb, 'fetch')
        release = await self.fetch_ost_latest()
        if not release:
            return False, '無法取得 GitHub release 資訊（網路問題或 API 限速）'

        asset = pick_release_zip(release)
        if not asset:
            return False, f'release {release.get("tag_name", "?")} 中找不到 zip 資源'

        tag = str(release.get('tag_name', ''))
        self.log.info(
            f'最新 OST 版本: {tag} - {asset["name"]} '
            f'({asset.get("size", 0)} bytes)'
        )
        zip_path = await self._download_asset(asset, progress_cb)
        if zip_path is None:
            asset_name = str(asset.get('name', ''))
            if Path(asset_name).name != asset_name or not asset_name.lower().endswith('.zip'):
                return False, 'release asset 檔名不安全'
            return False, f'下載 zip 失敗: {asset["browser_download_url"]}'

        try:
            self._progress(progress_cb, 'install', 0, 1)
            success, message, deployed = install_ost_from_zip(
                zip_path,
                steam_path,
                version=tag,
                runtime=self.runtime,
            )
            self._progress(progress_cb, 'install', 1, 1)
        finally:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass

        if not success:
            return False, message

        self.config['installed_ost_version'] = tag
        try:
            update_config_fields(
                {'installed_ost_version': tag},
                runtime=self.runtime,
            )
        except Exception as error:
            self.log.warning(f'寫入版本號失敗: {error}')
        return True, f'OST {tag} 安裝完成，共部署 {len(deployed)} 個檔案'

    def remove_ost(
        self,
        steam_path: Path | None,
        keep_installed_games: bool = True,
    ) -> tuple[bool, str, list[str]]:
        if not steam_path:
            return False, 'Steam 路徑未設定', []
        if self._steam_running():
            return False, 'Steam 仍在執行，請先完全關閉 Steam 後重試', []

        success, message, removed = uninstall_ost(
            steam_path,
            keep_installed_games=keep_installed_games,
            runtime=self.runtime,
        )
        if success:
            self.config['installed_ost_version'] = ''
            try:
                update_config_fields(
                    {'installed_ost_version': ''},
                    runtime=self.runtime,
                )
            except Exception:
                self.log.exception('清除 OST 版本紀錄失敗')
        return success, message, removed

    async def fetch_steamless_latest(self) -> dict[str, Any] | None:
        repo = str(self.config.get('Steamless_Repo', STEAMLESS_DEFAULT_REPO))
        async with self._client_scope() as client:
            return await fetch_latest_release(
                client,
                repo,
                allowed_repos={STEAMLESS_DEFAULT_REPO},
            )

    async def install_or_update_steamless(
        self,
        progress_cb: ProgressCallback | None = None,
    ) -> tuple[bool, str]:
        self._progress(progress_cb, 'fetch')
        release = await self.fetch_steamless_latest()
        if not release:
            return False, '無法取得 Steamless GitHub release 資訊（網路或 API 限速）'

        asset = pick_steamless_zip(release)
        if not asset:
            return False, f'release {release.get("tag_name", "?")} 中找不到 zip 資源'

        tag = str(release.get('tag_name', ''))
        self.log.info(
            f'最新 Steamless 版本: {tag} - {asset["name"]} '
            f'({asset.get("size", 0)} bytes)'
        )
        zip_path = await self._download_asset(asset, progress_cb)
        if zip_path is None:
            asset_name = str(asset.get('name', ''))
            if Path(asset_name).name != asset_name or not asset_name.lower().endswith('.zip'):
                return False, 'release asset 檔名不安全'
            return False, f'下載 zip 失敗: {asset["browser_download_url"]}'

        try:
            self._progress(progress_cb, 'install', 0, 1)
            success, message, deployed = install_steamless_from_zip(
                zip_path,
                version=tag,
                runtime=self.runtime,
            )
            self._progress(progress_cb, 'install', 1, 1)
        finally:
            try:
                zip_path.unlink(missing_ok=True)
            except OSError:
                pass

        if not success:
            return False, message

        self.config['installed_steamless_version'] = tag
        try:
            update_config_fields(
                {'installed_steamless_version': tag},
                runtime=self.runtime,
            )
        except Exception as error:
            self.log.warning(f'寫入 Steamless 版本號失敗: {error}')
        return True, f'Steamless {tag} 安裝完成，共部署 {len(deployed)} 個檔案'

    def remove_steamless(self) -> tuple[bool, str]:
        success, message = uninstall_steamless(runtime=self.runtime)
        if success:
            self.config['installed_steamless_version'] = ''
            try:
                update_config_fields(
                    {'installed_steamless_version': ''},
                    runtime=self.runtime,
                )
            except Exception:
                pass
        return success, message
