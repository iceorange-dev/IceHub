"""Transactional KRKRZ patch discovery, deployment, and removal."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Dict, Optional, Tuple

from ..constants import (
    KRKRZ_BUNDLED_PAYLOAD_REL,
    KRKRZ_BUNDLED_REL,
    KRKRZ_DLL_FILENAME,
    KRKRZ_REPO as KRKRZ_REPO,
)
from ..deployment import DeploymentError, ManagedFileDeployment, OwnershipError
from ..platform.files import _is_link_or_reparse, _validate_pe_file
from ..resources import resource_path
from ..runtime import BackendRuntime, resolve_runtime
from ..security import SecurityValidationError, sha256_file


def bundled_krkrz_dll(
    *, runtime: BackendRuntime | None = None
) -> Optional[Path]:
    """Locate the development DLL or Nuitka-safe staged payload.

    Nuitka excludes DLLs from generic data-directory includes. Release builds
    therefore carry the same verified PE bytes as ``Version.bin`` while local
    development continues to use the original ``Version.dll`` first.
    """

    for relative_path in (KRKRZ_BUNDLED_REL, KRKRZ_BUNDLED_PAYLOAD_REL):
        candidate = resource_path(relative_path, runtime=runtime)
        if candidate.is_file():
            return candidate
    return None


def detect_krkrz_status(
    target_dir: Path,
    *,
    runtime: BackendRuntime | None = None,
) -> Dict[str, bool]:
    """檢查目標目錄 KRKRZ 補丁狀態。
    回傳：{
      'deployed': 目錄下已有 (V|v)ersion.dll
      'has_backup': 是否存在 version.dll.bak[.N]
      'looks_like_krkrz': 是否有 KRKRZ 引擎跡象（data.xp3 / *.xp3 / startup.tjs 等）
    }
    """
    ctx = resolve_runtime(runtime)
    result = {
        'deployed': False,
        'has_backup': False,
        'looks_like_krkrz': False,
        'managed': False,
        'integrity_ok': False,
    }
    if not target_dir.exists() or not target_dir.is_dir():
        return result
    # Windows 不分大小寫
    for f in target_dir.iterdir():
        if not f.is_file():
            continue
        name = f.name.lower()
        if name.startswith('version.dll.bak'):
            result['has_backup'] = True
    # KRKRZ 引擎特徵
    krkrz_markers = ['data.xp3', 'patch.xp3', 'startup.tjs']
    if any((target_dir / m).exists() for m in krkrz_markers):
        result['looks_like_krkrz'] = True
    else:
        # 任意 *.xp3 也算
        if any(target_dir.glob('*.xp3')):
            result['looks_like_krkrz'] = True
    try:
        manager = ManagedFileDeployment(ctx.state_dir, 'krkrz', target_dir)
        managed = manager.status()
        result['managed'] = bool(managed['managed'])
        result['integrity_ok'] = bool(managed['healthy'])
        result['deployed'] = bool(managed['managed'] and managed['healthy'])
        if managed['managed']:
            result['has_backup'] = True
        if not result['deployed']:
            bundled = bundled_krkrz_dll(runtime=ctx)
            current = next(
                (f for f in target_dir.iterdir() if f.is_file() and f.name.lower() == 'version.dll'),
                None,
            )
            if bundled and current and sha256_file(current) == sha256_file(bundled):
                result['deployed'] = True  # legacy IceHub deployment, not yet adopted
                result['integrity_ok'] = True
    except (OSError, DeploymentError):
        result['integrity_ok'] = False
    return result


_KRKRZ_BACKUP_RE = re.compile(r'^version\.dll\.bak(?:\.(\d+))?$', re.IGNORECASE)


def _select_legacy_krkrz_original(target_dir: Path, patch_hash: str) -> Optional[Path]:
    """Select an unambiguous original from legacy numbered backups."""

    candidates: list[tuple[int, Path, str]] = []
    for path in target_dir.iterdir():
        match = _KRKRZ_BACKUP_RE.fullmatch(path.name)
        if not match:
            continue
        if _is_link_or_reparse(path) or not path.is_file():
            raise OwnershipError(f'KRKRZ legacy backup is linked or unsafe: {path}')
        _validate_pe_file(path)
        digest = sha256_file(path)
        if digest == patch_hash:
            continue
        order = int(match.group(1)) if match.group(1) is not None else 0
        candidates.append((order, path, digest))

    if not candidates:
        return None
    if len({digest for _order, _path, digest in candidates}) != 1:
        raise OwnershipError('找到多個內容不同的 KRKRZ 原檔備份，拒絕自動選擇')
    candidates.sort(key=lambda item: (item[0], item[1].name.casefold()))
    return candidates[0][1]


def deploy_krkrz_patch(
    target_dir: Path,
    backup_existing: bool = True,
    *,
    runtime: BackendRuntime | None = None,
) -> Tuple[bool, str, Optional[Path]]:
    """把內嵌 Version.dll 部署到 target_dir。
    若 target_dir/version.dll 已存在且 backup_existing=True，備份至 .bak[.N]
    回傳 (success, message, deployed_path)
    """
    ctx = resolve_runtime(runtime)
    src = bundled_krkrz_dll(runtime=ctx)
    if not src:
        return False, '找不到內嵌 Version.dll（資源未打包）', None
    if not target_dir.exists() or not target_dir.is_dir():
        return False, f'目標目錄不存在: {target_dir}', None

    try:
        _validate_pe_file(src)
        manager = ManagedFileDeployment(ctx.state_dir, 'krkrz', target_dir)
        current = next(
            (f for f in target_dir.iterdir() if f.is_file() and f.name.lower() == 'version.dll'),
            None,
        )
        overrides: Dict[str, Path] = {}
        created: set[str] = set()
        if current and not manager.manifest_path.exists():
            patch_hash = sha256_file(src)
            same_as_patch = sha256_file(current) == patch_hash
            if same_as_patch:
                original = _select_legacy_krkrz_original(target_dir, patch_hash)
                if original:
                    overrides[KRKRZ_DLL_FILENAME] = original
                else:
                    created.add(KRKRZ_DLL_FILENAME)
            elif not backup_existing:
                return False, '目標已有未受 IceHub 管理的 Version.dll；拒絕無備份覆寫', None

        result = manager.install(
            {KRKRZ_DLL_FILENAME: src},
            version=sha256_file(src)[:16],
            original_overrides=overrides,
            created_targets=created,
        )
        deployed = result.changed_files[0]
        return True, f'已交易式部署並記錄原檔 → {deployed}', deployed
    except PermissionError as e:
        return False, f'許可權不足: {e}', None
    except (SecurityValidationError, DeploymentError, OwnershipError) as e:
        return False, f'為保護原檔，部署已停止: {e}', None
    except Exception as e:
        return False, f'部署失敗: {e}', None


def remove_krkrz_patch(
    target_dir: Path,
    restore_backup: bool = True,
    *,
    runtime: BackendRuntime | None = None,
) -> Tuple[bool, str]:
    """從 target_dir 移除 Version.dll，可選擇還原最近一次備份"""
    ctx = resolve_runtime(runtime)
    if not target_dir.exists():
        return False, f'目標目錄不存在: {target_dir}'

    try:
        manager = ManagedFileDeployment(ctx.state_dir, 'krkrz', target_dir)
        if not manager.manifest_path.exists():
            current = next(
                (f for f in target_dir.iterdir() if f.is_file() and f.name.lower() == 'version.dll'),
                None,
            )
            if current is None:
                return True, '目錄內無 Version.dll，無需移除'
            bundled = bundled_krkrz_dll(runtime=ctx)
            if not bundled:
                return False, '找不到 IceHub 內嵌補丁，無法驗證目前 Version.dll'
            patch_hash = sha256_file(bundled)
            if sha256_file(current) != patch_hash:
                return False, 'Version.dll 並非 IceHub 內嵌補丁，拒絕刪除'
            original = _select_legacy_krkrz_original(target_dir, patch_hash)
            overrides = {KRKRZ_DLL_FILENAME: original} if original else {}
            created = set() if original else {KRKRZ_DLL_FILENAME}
            manager.install(
                {KRKRZ_DLL_FILENAME: current},
                version='legacy-migration',
                original_overrides=overrides,
                created_targets=created,
            )

        result = manager.uninstall()
        if not restore_backup:
            ctx.log.warning('安全模式會忽略「不還原備份」選項，原始 Version.dll 已還原')
        return True, f'已移除補丁並安全還原原檔（{result.changed_files[0].name}）'
    except PermissionError as e:
        return False, f'許可權不足（檔案被佔用？）: {e}'
    except (DeploymentError, OwnershipError) as e:
        return False, f'為保護外部檔案，移除已停止: {e}'
    except Exception as e:
        return False, f'移除失敗: {e}'

__all__ = [
    'KRKRZ_REPO',
    'bundled_krkrz_dll',
    'deploy_krkrz_patch',
    'detect_krkrz_status',
    'remove_krkrz_patch',
]
