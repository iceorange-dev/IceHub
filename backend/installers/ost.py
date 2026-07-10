"""OpenSteamTool detection and transactional deployment."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Dict, List, Tuple
import uuid
import zipfile

from ..constants import OST_FILES, TRUSTED_OST_FILE_HASHES
from ..deployment import (
    DeploymentError,
    ManagedFileDeployment,
    OwnershipError,
)
from ..platform.files import _is_link_or_reparse, _validate_pe_file
from ..runtime import BackendRuntime, resolve_runtime
from ..security import (
    SecurityValidationError,
    UnsafeArchiveError,
    normalize_zip_member,
    sha256_file,
)


def detect_ost(
    steam_path: Path,
    *,
    runtime: BackendRuntime | None = None,
) -> Dict[str, bool]:
    """檢查 Steam 目錄下是否有 OST 三件套"""

    ctx = resolve_runtime(runtime)
    manager = ManagedFileDeployment(ctx.state_dir, 'ost', steam_path)
    try:
        status = manager.status()
        if status['managed']:
            return {
                name: bool(status['files'].get(name, {}).get('matches'))
                for name in OST_FILES
            }
    except DeploymentError:
        return {name: False for name in OST_FILES}

    # Legacy installations have no manifest yet; require regular PE files.
    result: Dict[str, bool] = {}
    for name in OST_FILES:
        path = steam_path / name
        try:
            _validate_pe_file(path)
            result[name] = True
        except (OSError, SecurityValidationError):
            result[name] = False
    return result


def ost_installed(
    steam_path: Path,
    *,
    runtime: BackendRuntime | None = None,
) -> bool:
    """判斷 OST 是否已部署（OpenSteamTool.dll + 至少一個 loader 存在）"""

    status = detect_ost(steam_path, runtime=runtime)
    if not status.get('OpenSteamTool.dll'):
        return False
    loaders = ['dwmapi.dll', 'xinput1_4.dll']
    return any(status.get(loader, False) for loader in loaders)


def _prepare_legacy_ost_adoption(
    current: Dict[str, Path],
    backup_dir: Path,
) -> Tuple[Dict[str, Path], set[str]]:
    """Classify a pre-manifest OST install without guessing ownership.

    A distinct, valid legacy backup can safely serve as the original. Without
    one, IceHub will remove a DLL only when its hash is from the reviewed OST
    allowlist. Unknown files are left untouched and require manual review.
    """

    overrides: Dict[str, Path] = {}
    created: set[str] = set()
    for name, target in current.items():
        if _is_link_or_reparse(target):
            raise OwnershipError(
                f'legacy OST target is a link or reparse point: {target}'
            )
        _validate_pe_file(target)
        target_hash = sha256_file(target)
        backup = backup_dir / name
        if backup.exists() or backup.is_symlink():
            if _is_link_or_reparse(backup):
                raise OwnershipError(
                    f'legacy OST backup is a link or reparse point: {backup}'
                )
            _validate_pe_file(backup)
            if sha256_file(backup) == target_hash:
                raise OwnershipError(
                    f'{name} 的舊版備份與目前檔案相同，無法確認原檔，已保留兩者'
                )
            overrides[name] = backup
            continue
        if target_hash not in TRUSTED_OST_FILE_HASHES.get(name, set()):
            raise OwnershipError(
                f'{name} 沒有可驗證的原檔備份，且不是已知 IceHub/OST 版本；拒絕接管'
            )
        created.add(name)
    return overrides, created


def install_ost_from_zip(
    zip_path: Path,
    steam_path: Path,
    *,
    version: str = '',
    runtime: BackendRuntime | None = None,
) -> Tuple[bool, str, List[str]]:
    """從 zip 解壓 OST 三件套並部署到 Steam 根目錄。
    回傳 (success, message, deployed_files)
    """

    required = {'OpenSteamTool.dll', 'dwmapi.dll', 'xinput1_4.dll'}
    canonical = {name.lower(): name for name in required}
    deployed: List[str] = []

    if not zip_path.exists():
        return False, f'zip 不存在: {zip_path}', deployed
    if not steam_path.exists():
        return False, f'Steam 目錄不存在: {steam_path}', deployed

    ctx = resolve_runtime(runtime)
    staging = ctx.temp_dir / f'ost-stage-{uuid.uuid4().hex}'
    try:
        staging.mkdir(parents=True, exist_ok=False)
        with zipfile.ZipFile(zip_path, 'r') as archive:
            members = archive.infolist()
            found: Dict[str, zipfile.ZipInfo] = {}
            for member in members:
                if member.is_dir():
                    continue
                normalize_zip_member(member)
                base = Path(member.filename).name
                name = canonical.get(base.lower())
                if not name:
                    continue
                if name in found:
                    raise UnsafeArchiveError(f'zip 內有重複的必要檔案: {name}')
                if member.file_size <= 0 or member.file_size > 128 * 1024 * 1024:
                    raise UnsafeArchiveError(f'zip 內檔案大小不合理: {name}')
                found[name] = member

            missing = required - set(found.keys())
            if missing:
                return False, f'zip 中缺少: {", ".join(sorted(missing))}', deployed

            if sum(info.file_size for info in found.values()) > 256 * 1024 * 1024:
                raise UnsafeArchiveError('OST 壓縮檔解壓大小超過安全上限')

            staged_files: Dict[str, Path] = {}
            for name, info in found.items():
                staged = staging / name
                written = 0
                with archive.open(info) as source, staged.open('xb') as destination:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        if written > info.file_size or written > 128 * 1024 * 1024:
                            raise UnsafeArchiveError(f'{name} 解壓大小超出宣告')
                        destination.write(chunk)
                    destination.flush()
                    os.fsync(destination.fileno())
                if written != info.file_size:
                    raise UnsafeArchiveError(f'{name} 解壓後大小不符')
                _validate_pe_file(staged)
                staged_files[name] = staged

        manager = ManagedFileDeployment(ctx.state_dir, 'ost', steam_path)
        legacy_backup = steam_path / '.ost_backup'
        legacy_current = {
            name: steam_path / name
            for name in staged_files
            if (steam_path / name).exists() or (steam_path / name).is_symlink()
        }
        overrides, created = (
            _prepare_legacy_ost_adoption(legacy_current, legacy_backup)
            if not manager.manifest_path.exists()
            else ({}, set())
        )
        result = manager.install(
            staged_files,
            version=version,
            original_overrides=overrides,
            created_targets=created,
        )
        deployed = [str(path) for path in result.changed_files]
        for target in result.changed_files:
            ctx.log.info(f'已部署: {target}')
        return True, '已完成交易式部署並記錄檔案所有權', deployed
    except zipfile.BadZipFile:
        return False, '不是有效的 zip 檔案', deployed
    except (
        UnsafeArchiveError,
        SecurityValidationError,
        OwnershipError,
        DeploymentError,
    ) as error:
        return False, f'安全驗證或交易式部署失敗: {error}', deployed
    except PermissionError as error:
        return False, f'許可權不足，請關閉 Steam 後重試 ({error})', deployed
    except Exception as error:
        return False, f'安裝失敗: {error}', deployed
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def uninstall_ost(
    steam_path: Path,
    keep_installed_games: bool = True,
    *,
    runtime: BackendRuntime | None = None,
) -> Tuple[bool, str, List[str]]:
    """Transactionally restore files that IceHub replaced while installing OST.

    Args:
        steam_path: Steam 安裝目錄
        keep_installed_games: True 保留 config/lua/*.lua；False 一併清除

    Returns:
        (success, message, removed_files)
    """

    if not steam_path or not steam_path.exists():
        return False, 'Steam 目錄不存在', []

    ctx = resolve_runtime(runtime)
    manager = ManagedFileDeployment(ctx.state_dir, 'ost', steam_path)
    backup_dir = steam_path / '.ost_backup'
    try:
        if not manager.manifest_path.exists():
            # Migrate installations created by the pre-transactional IceHub.
            current = {
                name: steam_path / name
                for name in OST_FILES
                if (steam_path / name).is_file()
            }
            if not current:
                return True, 'OST 未安裝，無需解除安裝', []
            overrides, created = _prepare_legacy_ost_adoption(
                current, backup_dir
            )
            manager.install(
                current,
                version='legacy-migration',
                original_overrides=overrides,
                created_targets=created,
            )

        result = manager.uninstall()
        changed = [path.name for path in result.changed_files]

        # Legacy backup directories may contain unrelated user files and old
        # installer artifacts. They have no trustworthy ownership manifest,
        # so IceHub intentionally leaves the directory untouched.
        if not keep_installed_games:
            lua_count, lua_errors = _remove_all_managed_lua(
                steam_path / 'config' / 'lua', runtime=ctx
            )
            if lua_count:
                changed.append(f'IceHub 管理的 Lua ({lua_count} 個)')
            if lua_errors:
                return (
                    False,
                    'OST 已安全解除安裝，但部分 Lua 無法移除：'
                    + '；'.join(lua_errors),
                    changed,
                )

        # opensteamtool.toml and runtime logs are intentionally retained: they
        # were not created by this installer and may contain user settings.
        return True, f'已安全還原／移除 {len(changed)} 個項目', changed
    except PermissionError as error:
        return False, f'權限不足，請先完全關閉 Steam（{error}）', []
    except (DeploymentError, OwnershipError) as error:
        return False, f'為避免破壞外部修改，已停止解除安裝：{error}', []
    except Exception as error:
        return False, f'解除安裝失敗: {error}', []


def _remove_all_managed_lua(
    lua_dir: Path,
    *,
    runtime: BackendRuntime | None = None,
) -> Tuple[int, List[str]]:
    """Remove only Lua files with an IceHub ownership manifest."""

    ctx = resolve_runtime(runtime)
    if not lua_dir.is_dir():
        return 0, []
    removed = 0
    errors: List[str] = []
    for path in lua_dir.glob('*.lua'):
        if not path.stem.isascii() or not path.stem.isdigit():
            continue
        manager = ManagedFileDeployment(
            ctx.state_dir / 'library', f'lua-{path.stem}', lua_dir
        )
        if not manager.manifest_path.exists():
            continue
        try:
            manager.uninstall()
            removed += 1
        except DeploymentError as error:
            errors.append(f'{path.name}: {error}')
    return removed, errors


__all__ = [
    'detect_ost',
    'install_ost_from_zip',
    'ost_installed',
    'uninstall_ost',
]
