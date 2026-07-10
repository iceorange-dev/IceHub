"""Transactional Steamless ownership, installation, and removal lifecycle."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Dict, List, Optional, Tuple
import uuid
import zipfile

from ..constants import STEAMLESS_INTERNAL_MANIFEST
from ..deployment import DeploymentError, DirectorySwap, OwnershipError
from ..platform.files import _is_link_or_reparse, _validate_pe_file
from ..releases import pick_steamless_zip as pick_steamless_zip
from ..runtime import BackendRuntime, DEFAULT_RUNTIME, resolve_runtime
from ..security import (
    SecurityValidationError,
    UnsafeArchiveError,
    safe_extract_zip,
    safe_zip_destination,
    sha256_file,
)
from ..storage import AtomicJsonStore, CorruptJsonError, FileLock


STEAMLESS_DIR = DEFAULT_RUNTIME.steamless_dir
STEAMLESS_CLI = DEFAULT_RUNTIME.steamless_cli
STEAMLESS_MANIFEST = DEFAULT_RUNTIME.steamless_manifest
_steamless_manifest_store = DEFAULT_RUNTIME.steamless_manifest_store


def _steamless_lock_path(*, runtime: BackendRuntime | None = None) -> Path:
    return resolve_runtime(runtime).state_dir / 'locks' / 'steamless.lock'


def _steamless_remove_journal_path(*, runtime: BackendRuntime | None = None) -> Path:
    return resolve_runtime(runtime).state_dir / 'journals' / 'steamless-remove.json'


def _recover_steamless_removal(*, runtime: BackendRuntime | None = None) -> None:
    """Recover or finish a crash-interrupted Steamless uninstall."""

    ctx = resolve_runtime(runtime)
    steamless_dir = ctx.steamless_dir
    store = AtomicJsonStore(_steamless_remove_journal_path(runtime=ctx), default={})
    journal = store.read()
    if not journal:
        return
    if not isinstance(journal, dict) or journal.get('schema') != 1:
        raise OwnershipError('Steamless removal journal 格式無效')
    transaction_id = str(journal.get('transaction_id', ''))
    try:
        if uuid.UUID(transaction_id).hex != transaction_id:
            raise ValueError
    except ValueError as e:
        raise OwnershipError('Steamless removal journal transaction id 無效') from e
    recorded_target = Path(str(journal.get('target', ''))).resolve(strict=False)
    if recorded_target != steamless_dir.resolve(strict=False):
        raise OwnershipError('Steamless removal journal 指向不同目錄')
    phase = journal.get('phase')
    if phase not in {'prepared', 'moved', 'committed'}:
        raise OwnershipError('Steamless removal journal phase 無效')
    quarantine = steamless_dir.parent / f'.steamless-remove-{transaction_id}'
    target_exists = steamless_dir.exists() or steamless_dir.is_symlink()
    quarantine_exists = quarantine.exists() or quarantine.is_symlink()

    if phase in {'prepared', 'moved'}:
        if quarantine_exists:
            if target_exists:
                raise OwnershipError('Steamless removal recovery 發現目標與隔離目錄同時存在')
            if _is_link_or_reparse(quarantine):
                raise OwnershipError('Steamless removal quarantine 是不安全的 link/reparse point')
            os.replace(quarantine, steamless_dir)
        elif phase == 'moved' or not target_exists:
            raise OwnershipError('Steamless removal recovery 缺少原始目錄')
        store.path.unlink(missing_ok=True)
        return

    # A committed removal never restores the old executable tree.  It may
    # safely finish deleting only the deterministic quarantine it created.
    if quarantine_exists:
        if _is_link_or_reparse(quarantine):
            raise OwnershipError('Steamless removal quarantine 是不安全的 link/reparse point')
        shutil.rmtree(quarantine, ignore_errors=False)
    try:
        ctx.steamless_manifest_store.write({})
    except OSError:
        pass
    store.path.unlink(missing_ok=True)


def _read_steamless_internal_manifest(
    *, runtime: BackendRuntime | None = None
) -> Dict:
    path = resolve_runtime(runtime).steamless_dir / STEAMLESS_INTERNAL_MANIFEST
    if not path.is_file() or _is_link_or_reparse(path):
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as e:
        raise OwnershipError(f'Steamless 內部 ownership manifest 無法讀取: {e}') from e
    if not isinstance(value, dict):
        raise OwnershipError('Steamless 內部 ownership manifest 格式無效')
    return value


def _validate_steamless_ownership(
    *, runtime: BackendRuntime | None = None
) -> Dict:
    """Return a verified ownership manifest for the currently installed tree."""

    # The external state file is only a UI/version cache and may outlive a
    # directory replacement.  Destructive authorization always comes from the
    # manifest committed atomically inside the installed tree.
    ctx = resolve_runtime(runtime)
    steamless_dir = ctx.steamless_dir
    manifest = _read_steamless_internal_manifest(runtime=ctx)
    if not manifest:
        raise OwnershipError('Steamless 目錄沒有 IceHub ownership manifest')
    if (
        manifest.get('schema') != 1
        or manifest.get('product') != 'steamless'
        or not isinstance(manifest.get('files'), dict)
    ):
        raise OwnershipError('Steamless ownership manifest 無效')

    expected: Dict[str, str] = {}
    seen: set[str] = set()
    for raw_relative, raw_hash in manifest['files'].items():
        relative = str(raw_relative)
        digest = str(raw_hash).lower()
        if re.fullmatch(r'[0-9a-f]{64}', digest) is None:
            raise OwnershipError(f'Steamless manifest hash 無效: {relative}')
        try:
            target = safe_zip_destination(steamless_dir, relative)
        except UnsafeArchiveError as e:
            raise OwnershipError(f'Steamless manifest 路徑不安全: {relative}') from e
        key = relative.replace('\\', '/').casefold()
        if key in seen:
            raise OwnershipError('Steamless manifest 含大小寫碰撞路徑')
        seen.add(key)
        if _is_link_or_reparse(target) or not target.is_file():
            raise OwnershipError(f'Steamless 檔案遺失或是連結: {relative}')
        if sha256_file(target) != digest:
            raise OwnershipError(f'Steamless 檔案已被外部修改: {relative}')
        expected[relative.replace('\\', '/')] = digest

    actual: set[str] = set()
    for path in steamless_dir.rglob('*'):
        if _is_link_or_reparse(path):
            raise OwnershipError(f'Steamless 目錄包含連結或 reparse point: {path}')
        if path.is_file() and path != steamless_dir / STEAMLESS_INTERNAL_MANIFEST:
            actual.add(str(path.relative_to(steamless_dir)).replace('\\', '/'))
    if actual != set(expected):
        raise OwnershipError('Steamless 目錄內容與 ownership manifest 不一致')
    return manifest

def is_steamless_installed(*, runtime: BackendRuntime | None = None) -> bool:
    """檢查 Steamless.CLI.exe 是否存在於 tools/steamless/"""
    try:
        ctx = resolve_runtime(runtime)
        with FileLock(_steamless_lock_path(runtime=ctx), timeout=5):
            _recover_steamless_removal(runtime=ctx)
            _validate_steamless_ownership(runtime=ctx)
            _validate_pe_file(ctx.steamless_cli)
        return True
    except (OSError, SecurityValidationError, OwnershipError, CorruptJsonError):
        return False


def install_steamless_from_zip(
    zip_path: Path,
    *,
    version: str = '',
    runtime: BackendRuntime | None = None,
) -> Tuple[bool, str, List[str]]:
    """解壓 Steamless zip 到 tools/steamless/。
    需要至少包含 Steamless.CLI.exe + Plugins/*.dll。
    回傳 (success, message, deployed_files)
    """
    deployed: List[str] = []
    ctx = resolve_runtime(runtime)
    steamless_dir = ctx.steamless_dir
    if not zip_path.exists():
        return False, f'zip 不存在: {zip_path}', deployed

    extraction = steamless_dir.parent / f'.steamless-extract-{uuid.uuid4().hex}'
    swap_staging: Optional[Path] = None
    try:
        steamless_dir.parent.mkdir(parents=True, exist_ok=True)
        extracted = safe_extract_zip(
            zip_path,
            extraction,
            max_files=2_000,
            max_file_size=128 * 1024 * 1024,
            max_total_size=512 * 1024 * 1024,
        )
        cli_candidates = [p for p in extracted if p.name.lower() == 'steamless.cli.exe']
        if len(cli_candidates) != 1:
            raise UnsafeArchiveError('zip 必須恰好包含一個 Steamless.CLI.exe')
        cli = cli_candidates[0]
        package_root = cli.parent
        plugins_root = package_root / 'Plugins'
        plugin_files = [
            p for p in extracted
            if p.suffix.lower() == '.dll'
            and plugins_root in p.parents
        ]
        if not plugin_files:
            raise UnsafeArchiveError('zip 中缺少 Steamless Plugins/*.dll')
        _validate_pe_file(cli)
        for plugin in plugin_files:
            _validate_pe_file(plugin)

        if package_root == extraction.resolve(strict=True):
            swap_staging = extraction
        else:
            swap_staging = steamless_dir.parent / f'.steamless-stage-{uuid.uuid4().hex}'
            os.replace(package_root, swap_staging)

        internal_manifest = swap_staging / STEAMLESS_INTERNAL_MANIFEST
        reserved_conflicts = [
            path for path in swap_staging.rglob('*')
            if path.name.casefold() == STEAMLESS_INTERNAL_MANIFEST.casefold()
        ]
        if reserved_conflicts:
            raise UnsafeArchiveError(
                f'zip 不得包含保留檔名 {STEAMLESS_INTERNAL_MANIFEST}'
            )
        staged_paths = sorted(path for path in swap_staging.rglob('*') if path.is_file())
        manifest = {
            'schema': 1,
            'product': 'steamless',
            'version': str(version),
            'installed_at': time.time(),
            'files': {
                str(path.relative_to(swap_staging)).replace('\\', '/'): sha256_file(path)
                for path in staged_paths
            },
        }
        with internal_manifest.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())

        with FileLock(_steamless_lock_path(runtime=ctx), timeout=30):
            _recover_steamless_removal(runtime=ctx)
            if steamless_dir.exists() or steamless_dir.is_symlink():
                if _is_link_or_reparse(steamless_dir):
                    raise OwnershipError('Steamless 目錄是 link/reparse point，拒絕覆寫')
                _validate_steamless_ownership(runtime=ctx)
            with DirectorySwap(swap_staging, steamless_dir) as swap:
                deployed_cli = steamless_dir / 'Steamless.CLI.exe'
                _validate_pe_file(deployed_cli)
                _validate_steamless_ownership(runtime=ctx)
                swap.commit()
            try:
                ctx.steamless_manifest_store.write(manifest)
            except OSError as e:
                # The in-directory manifest was committed atomically with the
                # tool.  This external copy is only a UI/version cache.
                ctx.log.warning(f'無法更新 Steamless manifest 快取: {e}')
        deployed_paths = [steamless_dir / path.relative_to(swap_staging) for path in staged_paths]
        deployed = [str(path) for path in deployed_paths]
        return True, '已安全解壓並以交易方式切換工具目錄', deployed
    except zipfile.BadZipFile:
        return False, '不是有效的 zip 檔案', deployed
    except (UnsafeArchiveError, SecurityValidationError, DeploymentError) as e:
        return False, f'安全驗證或交易式部署失敗: {e}', deployed
    except PermissionError as e:
        return False, f'許可權不足: {e}', deployed
    except Exception as e:
        return False, f'安裝失敗: {e}', deployed
    finally:
        if extraction.exists() and extraction != steamless_dir:
            shutil.rmtree(extraction, ignore_errors=True)
        if swap_staging and swap_staging.exists() and swap_staging != steamless_dir:
            shutil.rmtree(swap_staging, ignore_errors=True)


def uninstall_steamless(
    *, runtime: BackendRuntime | None = None
) -> Tuple[bool, str]:
    """整個 tools/steamless 目錄移除"""
    ctx = resolve_runtime(runtime)
    steamless_dir = ctx.steamless_dir
    transaction_id = uuid.uuid4().hex
    quarantine = steamless_dir.parent / f'.steamless-remove-{transaction_id}'
    journal_store = AtomicJsonStore(
        _steamless_remove_journal_path(runtime=ctx), default={}
    )
    try:
        with FileLock(_steamless_lock_path(runtime=ctx), timeout=30):
            _recover_steamless_removal(runtime=ctx)
            if not steamless_dir.exists() and not steamless_dir.is_symlink():
                return True, 'Steamless 未安裝'
            if _is_link_or_reparse(steamless_dir):
                raise OwnershipError('Steamless 目錄是 link/reparse point，拒絕刪除')
            _validate_steamless_ownership(runtime=ctx)
            try:
                journal = {
                    'schema': 1,
                    'target': str(steamless_dir.resolve(strict=False)),
                    'transaction_id': transaction_id,
                    'phase': 'prepared',
                }
                journal_store.write(journal)
                os.replace(steamless_dir, quarantine)
                journal['phase'] = 'moved'
                journal_store.write(journal)
                journal['phase'] = 'committed'
                journal_store.write(journal)
            except Exception as operation_error:
                try:
                    _recover_steamless_removal(runtime=ctx)
                except Exception as rollback_error:
                    return False, (
                        f'移除失敗且自動還原失敗: {operation_error}；{rollback_error}；'
                        f'隔離目錄: {quarantine}'
                    )
                raise

            try:
                _recover_steamless_removal(runtime=ctx)
            except Exception as cleanup_error:
                return True, (
                    f'工具已卸載，但隔離目錄稍後需清理: {quarantine} '
                    f'({cleanup_error})'
                )
        return True, f'已移除 {steamless_dir}'
    except (OwnershipError, UnsafeArchiveError, CorruptJsonError) as e:
        return False, f'為保護非 IceHub 檔案，拒絕移除: {e}'
    except PermissionError as e:
        return False, f'許可權不足: {e}'
    except Exception as e:
        return False, f'移除失敗: {e}'



__all__ = [
    'STEAMLESS_CLI',
    'STEAMLESS_DIR',
    'STEAMLESS_MANIFEST',
    'install_steamless_from_zip',
    'is_steamless_installed',
    'pick_steamless_zip',
    'uninstall_steamless',
]
