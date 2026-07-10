"""Steamless process execution and verified executable replacement."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, Optional, Tuple
import uuid

import psutil

from ..constants import STEAMLESS_FLAGS
from ..deployment import OwnershipError
from ..platform.files import _is_link_or_reparse, _validate_pe_file
from ..runtime import BackendRuntime, resolve_runtime
from ..security import SecurityValidationError, sha256_file
from ..storage import CorruptJsonError, FileLock
from .steamless import _steamless_lock_path, _validate_steamless_ownership


def _hidden_subprocess_kwargs() -> Dict:
    """產生 subprocess kwargs，隱藏 CMD 窗口（給 .NET CLI 之類工具用）。"""

    kwargs: Dict = {}
    if sys.platform == 'win32':
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = subprocess.SW_HIDE
        kwargs['startupinfo'] = startup_info
        kwargs['creationflags'] = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    return kwargs


def unpack_with_steamless(
    exe_path: Path,
    options: Dict[str, bool],
    output_cb: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    timeout_seconds: float = 10 * 60,
    *,
    runtime: BackendRuntime | None = None,
) -> Tuple[bool, int, Optional[Path]]:
    """執行 Steamless CLI，並回傳成功狀態、exit code 與輸出路徑。"""

    ctx = resolve_runtime(runtime)
    steamless_cli = ctx.steamless_cli
    steamless_dir = ctx.steamless_dir
    if not steamless_cli.exists():
        return False, -1, None
    if not exe_path.exists() or not exe_path.is_file():
        if output_cb:
            output_cb(f'[error] 檔案不存在: {exe_path}')
        return False, -1, None
    if timeout_seconds <= 0:
        if output_cb:
            output_cb('[error] timeout_seconds 必須大於 0')
        return False, -1, None

    output = exe_path.parent / (exe_path.name + '.unpacked.exe')
    if output.exists():
        stale = _pick_backup_path(output)
        try:
            os.replace(output, stale)
            if output_cb:
                output_cb(f'[backup] 既有輸出已移至 {stale.name}')
        except OSError as error:
            if output_cb:
                output_cb(f'[error] 無法隔離既有輸出: {error}')
            return False, -1, None

    args = [str(steamless_cli)]
    for key, flag, _default, _description in STEAMLESS_FLAGS:
        if options.get(key):
            args.append(flag)
    args.append(str(exe_path))

    if output_cb:
        output_cb(f'> {" ".join(args)}')

    kwargs = _hidden_subprocess_kwargs()
    try:
        with FileLock(_steamless_lock_path(runtime=ctx), timeout=30):
            if runtime is None:
                _validate_steamless_ownership()
            else:
                _validate_steamless_ownership(runtime=ctx)
            proc = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(steamless_dir),
                text=True,
                encoding='utf-8',
                errors='replace',
                bufsize=1,
                **kwargs,
            )
    except (OwnershipError, CorruptJsonError) as error:
        if output_cb:
            output_cb(f'[error] Steamless 完整性驗證失敗: {error}')
        return False, -1, None
    except FileNotFoundError as error:
        if output_cb:
            output_cb(f'[error] 無法啟動 Steamless.CLI.exe: {error}')
        return False, -1, None
    except Exception as error:
        if output_cb:
            output_cb(f'[error] 啟動失敗: {error}')
        return False, -1, None

    def stop_process(*, force: bool = False) -> None:
        try:
            root = psutil.Process(proc.pid)
            descendants = root.children(recursive=True)
            for child in descendants:
                try:
                    child.kill() if force else child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            if force:
                root.kill()
            else:
                root.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            try:
                proc.kill() if force else proc.terminate()
            except OSError:
                pass

    line_queue: queue.Queue[Optional[str]] = queue.Queue()

    def read_output() -> None:
        try:
            if proc.stdout is not None:
                for line in iter(proc.stdout.readline, ''):
                    line_queue.put(line)
        finally:
            line_queue.put(None)

    reader = threading.Thread(
        target=read_output,
        name='steamless-output',
        daemon=True,
    )
    reader.start()
    deadline = time.monotonic() + timeout_seconds
    reader_done = False
    try:
        while True:
            if cancel_check and cancel_check():
                stop_process()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    stop_process(force=True)
                    proc.wait(timeout=5)
                if output_cb:
                    output_cb('[cancelled] 使用者取消')
                return False, -2, None
            if time.monotonic() >= deadline:
                stop_process()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    stop_process(force=True)
                    proc.wait(timeout=5)
                if output_cb:
                    output_cb(f'[timeout] 超過 {timeout_seconds:.0f} 秒，已終止程序')
                return False, -3, None

            try:
                line = line_queue.get(timeout=0.1)
                if line is None:
                    reader_done = True
                elif output_cb:
                    output_cb(line.rstrip())
            except queue.Empty:
                pass

            if proc.poll() is not None and reader_done and line_queue.empty():
                break
        proc.wait(timeout=5)
    except Exception as error:
        if output_cb:
            output_cb(f'[error] 讀取輸出失敗: {error}')
        stop_process(force=True)
        return False, -1, None

    if proc.returncode == 0 and output.exists():
        try:
            _validate_pe_file(
                output,
                max_size=max(
                    exe_path.stat().st_size * 2,
                    128 * 1024 * 1024,
                ),
            )
            return True, 0, output
        except (OSError, SecurityValidationError) as error:
            if output_cb:
                output_cb(f'[error] 輸出驗證失敗: {error}')
            return False, -1, output
    return False, proc.returncode, output if output.exists() else None


def _pick_backup_path(original: Path) -> Path:
    """為 *original* 找一個不衝突的 ``.bak[.N]`` 路徑。"""

    backup = original.with_name(original.name + '.bak')
    if not backup.exists():
        return backup
    index = 1
    while True:
        candidate = original.with_name(f'{original.name}.bak.{index}')
        if not candidate.exists():
            return candidate
        index += 1


def replace_with_unpacked(
    original: Path,
    unpacked: Path,
    *,
    runtime: BackendRuntime | None = None,
) -> Tuple[bool, str, Optional[Path]]:
    """Atomically replace an executable after publishing a verified backup."""

    original = Path(original).absolute()
    unpacked = Path(unpacked).absolute()
    if not original.exists():
        return False, f'原檔不存在: {original}', None
    if not unpacked.exists():
        return False, f'脫殼產物不存在: {unpacked}', None
    if _is_link_or_reparse(original) or _is_link_or_reparse(unpacked):
        return False, '原檔或脫殼產物是 link/reparse point，拒絕覆蓋', None
    if original.resolve() == unpacked.resolve():
        return False, '原檔與脫殼產物路徑相同，拒絕覆蓋', None

    ctx = resolve_runtime(runtime)
    lock_key = hashlib.sha256(
        os.path.normcase(str(original.resolve(strict=False))).encode('utf-8')
    ).hexdigest()[:20]
    lock_path = ctx.state_dir / 'locks' / f'executable-replace-{lock_key}.lock'
    backup: Optional[Path] = None
    replacement_published = False
    try:
        with FileLock(lock_path, timeout=30):
            _validate_pe_file(original)
            _validate_pe_file(unpacked)
            original_hash = sha256_file(original)
            unpacked_hash = sha256_file(unpacked)

            for _attempt in range(1_000):
                candidate = _pick_backup_path(original)
                try:
                    with original.open('rb') as src, candidate.open('xb') as dst:
                        shutil.copyfileobj(src, dst, length=1024 * 1024)
                        dst.flush()
                        os.fsync(dst.fileno())
                    shutil.copystat(original, candidate, follow_symlinks=False)
                    backup = candidate
                    break
                except FileExistsError:
                    continue
                except Exception:
                    candidate.unlink(missing_ok=True)
                    raise
            if backup is None:
                raise OSError('無法配置不衝突的備份檔名')
            if sha256_file(backup) != original_hash:
                backup.unlink(missing_ok=True)
                backup = None
                raise OSError('原檔備份驗證失敗')

            os.replace(unpacked, original)
            replacement_published = True
            if sha256_file(original) != unpacked_hash:
                raise OSError('替換後檔案 hash 與脫殼產物不符')
            _validate_pe_file(original)

        assert backup is not None
        return True, f'已備份原檔 → {backup.name}，並以脫殼版原子覆蓋', backup
    except Exception as error:
        restore_error: Optional[Exception] = None
        if replacement_published and backup and backup.is_file():
            restore_temp = original.with_name(
                f'.{original.name}.icehub-restore-{uuid.uuid4().hex}.tmp'
            )
            try:
                with backup.open('rb') as src, restore_temp.open('xb') as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                    dst.flush()
                    os.fsync(dst.fileno())
                os.replace(restore_temp, original)
                if sha256_file(original) != sha256_file(backup):
                    raise OSError('還原後 hash 驗證失敗')
            except Exception as restore_failure:
                restore_error = restore_failure
            finally:
                restore_temp.unlink(missing_ok=True)

        suffix = f'；已保留驗證備份 {backup}' if backup else ''
        if restore_error is not None:
            suffix += f'；自動還原失敗: {restore_error}'
        elif replacement_published:
            suffix += '；已從備份還原原檔'
        prefix = (
            '許可權不足（檔案可能被佔用）'
            if isinstance(error, PermissionError)
            else '替換失敗'
        )
        return False, f'{prefix}: {error}{suffix}', backup


__all__ = [
    '_hidden_subprocess_kwargs',
    '_pick_backup_path',
    'replace_with_unpacked',
    'unpack_with_steamless',
]
