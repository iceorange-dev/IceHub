"""Contract tests for the extracted Steamless runner facade."""

from __future__ import annotations

from pathlib import Path

import backend.ost_backend as backend_facade
from backend.installers import steamless_runner


def test_backend_facade_statically_reexports_runner_functions() -> None:
    assert (
        backend_facade.unpack_with_steamless
        is steamless_runner.unpack_with_steamless
    )
    assert (
        backend_facade.replace_with_unpacked
        is steamless_runner.replace_with_unpacked
    )
    assert (
        backend_facade._hidden_subprocess_kwargs
        is steamless_runner._hidden_subprocess_kwargs
    )
    assert backend_facade._pick_backup_path is steamless_runner._pick_backup_path


def test_backup_path_numbering_remains_deterministic(tmp_path: Path) -> None:
    executable = tmp_path / 'game.exe'
    (tmp_path / 'game.exe.bak').write_bytes(b'first')
    (tmp_path / 'game.exe.bak.1').write_bytes(b'second')

    assert steamless_runner._pick_backup_path(executable) == (
        tmp_path / 'game.exe.bak.2'
    )
