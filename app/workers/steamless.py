"""Sequential Steamless unpack worker."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

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


class SteamlessUnpackWorker(QThread):
    """Process files sequentially and stream Steamless output to the UI."""

    line = pyqtSignal(str)
    one_done = pyqtSignal(str, bool, str, str)
    all_done = pyqtSignal(int, int)

    def __init__(
        self,
        files,
        options,
        replace_original: bool = False,
        *,
        runtime: BackendRuntime | None = None,
    ):
        super().__init__()
        self.runtime = resolve_runtime(runtime)
        self.files = [Path(file) for file in files]
        self.options = dict(options or {})
        self.replace_original = bool(replace_original)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True

    def run(self):
        success = 0
        fail = 0
        total = len(self.files)
        try:
            for index, file in enumerate(self.files):
                if self._cancelled:
                    self.line.emit('[cancelled] 已取消後續檔案')
                    break
                self.line.emit('')
                self.line.emit(
                    f'========== [{index + 1}/{total}] {file.name} =========='
                )
                try:
                    ok, code, output = unpack_with_steamless(
                        file,
                        self.options,
                        output_cb=self.line.emit,
                        cancel_check=lambda: self._cancelled,
                        runtime=self.runtime,
                    )
                    if ok and output is not None:
                        self.line.emit(f'[ok] 脫殼產物: {output}')
                        final_path = output
                        if self.replace_original:
                            (
                                replace_ok,
                                replace_message,
                                _backup,
                            ) = replace_with_unpacked(
                                file,
                                output,
                                runtime=self.runtime,
                            )
                            if replace_ok:
                                self.line.emit(f'[replace] {replace_message}')
                                final_path = file
                            else:
                                self.line.emit(
                                    f'[replace fail] {replace_message}'
                                    f'（脫殼產物保留為 {output.name}）'
                                )
                        success += 1
                        self.one_done.emit(
                            str(file),
                            True,
                            f'成功 → {final_path.name}',
                            str(final_path),
                        )
                    else:
                        fail += 1
                        reason = (
                            '已取消' if code == -2 else f'失敗 (exit {code})'
                        )
                        self.line.emit(f'[fail] {reason}')
                        self.one_done.emit(str(file), False, reason, '')
                except Exception as error:
                    fail += 1
                    reason = _unexpected_failure(error)
                    self.line.emit(f'[fail] {reason}')
                    self.one_done.emit(str(file), False, reason, '')
        finally:
            self.all_done.emit(success, fail)


__all__ = ['SteamlessUnpackWorker']
