"""Sequential KRKRZ patch worker with optional Steamless preprocessing."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from backend.installers.krkrz import deploy_krkrz_patch, remove_krkrz_patch
from backend.installers.steamless import is_steamless_installed
from backend.installers.steamless_runner import (
    replace_with_unpacked,
    unpack_with_steamless,
)
from backend.runtime import BackendRuntime, resolve_runtime


def _unexpected_failure(error: Exception) -> str:
    detail = str(error).strip()
    description = type(error).__name__
    if detail:
        description += f': {detail}'
    return f'未預期錯誤 ({description})'


class KrkrzWorker(QThread):
    """Deploy or remove Version.dll for a sequence of game executables."""

    line = pyqtSignal(str)
    one_done = pyqtSignal(str, bool, str)
    all_done = pyqtSignal(int, int)

    def __init__(
        self,
        exes,
        mode: str,
        backup_existing: bool = True,
        restore_backup: bool = True,
        use_steamless: bool = False,
        *,
        runtime: BackendRuntime | None = None,
    ):
        super().__init__()
        self.runtime = resolve_runtime(runtime)
        self.exes = [Path(executable) for executable in exes]
        self.mode = mode
        self.backup_existing = backup_existing
        self.restore_backup = restore_backup
        self.use_steamless = use_steamless
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def _steamless_pre_unpack(self, executable: Path) -> None:
        if not is_steamless_installed(runtime=self.runtime):
            self.line.emit('[steamless] 未安裝，跳過預處理')
            return
        self.line.emit(f'[steamless] 嘗試 {executable.name}')
        ok, code, output = unpack_with_steamless(
            executable,
            {'realign': True, 'recalcchecksum': True},
            output_cb=lambda line: self.line.emit(f'  | {line}'),
            cancel_check=lambda: self._cancelled,
            runtime=self.runtime,
        )
        if ok and output is not None:
            replace_ok, replace_message, _backup = replace_with_unpacked(
                executable,
                output,
                runtime=self.runtime,
            )
            if replace_ok:
                self.line.emit(f'[steamless] ✓ {replace_message}')
            else:
                self.line.emit(
                    f'[steamless] ⚠ 脫殼成功但覆蓋失敗: {replace_message}'
                )
        else:
            self.line.emit(
                f'[steamless] 跳過（無 SteamStub 或脫殼失敗，exit {code}）'
            )

    def run(self):
        success = 0
        fail = 0
        total = len(self.exes)
        try:
            for index, executable in enumerate(self.exes):
                if self._cancelled:
                    self.line.emit('[cancelled] 已取消後續項目')
                    break
                deploy_dir = executable.parent
                self.line.emit('')
                self.line.emit(
                    f'========== [{index + 1}/{total}] {executable.name} =========='
                )
                self.line.emit(f'目錄: {deploy_dir}')

                try:
                    if self.mode == 'deploy':
                        if self.use_steamless:
                            self._steamless_pre_unpack(executable)
                        ok, message, _deployed = deploy_krkrz_patch(
                            deploy_dir,
                            backup_existing=self.backup_existing,
                            runtime=self.runtime,
                        )
                    else:
                        ok, message = remove_krkrz_patch(
                            deploy_dir,
                            restore_backup=self.restore_backup,
                            runtime=self.runtime,
                        )
                except Exception as error:
                    ok = False
                    message = _unexpected_failure(error)

                tag = '[ok]' if ok else '[fail]'
                self.line.emit(f'{tag} {message}')
                if ok:
                    success += 1
                else:
                    fail += 1
                self.one_done.emit(str(executable), ok, message)
        finally:
            self.all_done.emit(success, fail)


__all__ = ['KrkrzWorker']
