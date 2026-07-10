"""Focused tests for page workers extracted from the monolithic UI module."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.workers import KrkrzWorker, SteamlessUnpackWorker
from app.workers import krkrz as krkrz_module
from app.workers import steamless as steamless_module
from backend.runtime import BackendRuntime
from backend.storage import AppPaths


def _runtime(tmp_path: Path) -> BackendRuntime:
    return BackendRuntime.from_paths(
        AppPaths.discover(development_root=tmp_path / 'runtime')
    )


def test_workers_package_exports_static_worker_classes() -> None:
    assert SteamlessUnpackWorker is steamless_module.SteamlessUnpackWorker
    assert KrkrzWorker is krkrz_module.KrkrzWorker


def test_steamless_worker_constructor_and_cancel(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    options = {'realign': True}
    worker = SteamlessUnpackWorker(
        [str(tmp_path / 'game.exe')],
        options,
        replace_original=True,
    )
    options['realign'] = False
    lines: list[str] = []
    totals: list[tuple[int, int]] = []
    worker.line.connect(lines.append)
    worker.all_done.connect(lambda success, fail: totals.append((success, fail)))

    worker.cancel()
    worker.run()

    assert worker.files == [tmp_path / 'game.exe']
    assert worker.options == {'realign': True}
    assert worker.replace_original is True
    assert worker._cancelled is True
    assert lines == ['[cancelled] 已取消後續檔案']
    assert totals == [(0, 0)]


def test_steamless_worker_runs_and_replaces_successful_output(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / 'game.exe'
    unpacked = tmp_path / 'game.exe.unpacked.exe'
    worker_runtime = _runtime(tmp_path)
    calls: list[tuple[str, object]] = []

    def unpack(file, options, *, output_cb, cancel_check, runtime=None):
        assert runtime is worker_runtime
        calls.append(('unpack', (file, options.copy(), cancel_check())))
        output_cb('runner output')
        return True, 0, unpacked

    def replace(original, output, *, runtime=None):
        assert runtime is worker_runtime
        calls.append(('replace', (original, output)))
        return True, 'replacement complete', tmp_path / 'game.exe.bak'

    monkeypatch.setattr(steamless_module, 'unpack_with_steamless', unpack)
    monkeypatch.setattr(steamless_module, 'replace_with_unpacked', replace)
    worker = SteamlessUnpackWorker(
        [executable],
        {'realign': True},
        replace_original=True,
        runtime=worker_runtime,
    )
    lines: list[str] = []
    completed: list[tuple[str, bool, str, str]] = []
    totals: list[tuple[int, int]] = []
    worker.line.connect(lines.append)
    worker.one_done.connect(
        lambda path, ok, message, final: completed.append(
            (path, ok, message, final)
        )
    )
    worker.all_done.connect(lambda success, fail: totals.append((success, fail)))

    worker.run()

    assert calls == [
        ('unpack', (executable, {'realign': True}, False)),
        ('replace', (executable, unpacked)),
    ]
    assert 'runner output' in lines
    assert '[replace] replacement complete' in lines
    assert completed == [
        (str(executable), True, '成功 → game.exe', str(executable))
    ]
    assert totals == [(1, 0)]


def test_steamless_worker_converts_domain_exception_and_continues(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / 'first.exe'
    second = tmp_path / 'second.exe'
    unpacked = tmp_path / 'second.exe.unpacked.exe'

    def unpack(file, _options, *, output_cb, cancel_check, runtime=None):
        del output_cb, cancel_check, runtime
        if file == first:
            raise RuntimeError('runner exploded')
        return True, 0, unpacked

    monkeypatch.setattr(steamless_module, 'unpack_with_steamless', unpack)
    worker = SteamlessUnpackWorker([first, second], {})
    lines: list[str] = []
    completed: list[tuple[str, bool, str, str]] = []
    totals: list[tuple[int, int]] = []
    worker.line.connect(lines.append)
    worker.one_done.connect(
        lambda path, ok, message, output: completed.append(
            (path, ok, message, output)
        )
    )
    worker.all_done.connect(lambda success, fail: totals.append((success, fail)))

    worker.run()

    assert completed[0][0] == str(first)
    assert completed[0][1] is False
    assert 'RuntimeError: runner exploded' in completed[0][2]
    assert completed[0][3] == ''
    assert completed[1] == (
        str(second),
        True,
        '成功 → second.exe.unpacked.exe',
        str(unpacked),
    )
    assert any(line.startswith('[fail] 未預期錯誤') for line in lines)
    assert totals == [(1, 1)]


def test_steamless_worker_emits_totals_even_when_fatal_error_escapes(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qtbot

    class SimulatedFatalError(BaseException):
        pass

    def crash(*_args, **_kwargs):
        raise SimulatedFatalError('simulated thread abort')

    monkeypatch.setattr(steamless_module, 'unpack_with_steamless', crash)
    worker = SteamlessUnpackWorker([tmp_path / 'game.exe'], {})
    totals: list[tuple[int, int]] = []
    worker.all_done.connect(lambda success, fail: totals.append((success, fail)))

    with pytest.raises(SimulatedFatalError, match='simulated thread abort'):
        worker.run()

    assert totals == [(0, 0)]


def test_krkrz_worker_constructor_and_cancel(
    qtbot: Any,
    tmp_path: Path,
) -> None:
    executable = tmp_path / 'game.exe'
    worker = KrkrzWorker(
        [str(executable)],
        'remove',
        backup_existing=False,
        restore_backup=False,
        use_steamless=True,
    )
    lines: list[str] = []
    totals: list[tuple[int, int]] = []
    worker.line.connect(lines.append)
    worker.all_done.connect(lambda success, fail: totals.append((success, fail)))

    worker.cancel()
    worker.run()

    assert worker.exes == [executable]
    assert worker.mode == 'remove'
    assert worker.backup_existing is False
    assert worker.restore_backup is False
    assert worker.use_steamless is True
    assert lines == ['[cancelled] 已取消後續項目']
    assert totals == [(0, 0)]


def test_krkrz_worker_reports_mixed_deploy_results(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / 'one' / 'game.exe'
    second = tmp_path / 'two' / 'game.exe'
    worker_runtime = _runtime(tmp_path)
    calls: list[tuple[Path, bool]] = []

    def deploy(directory: Path, *, backup_existing: bool, runtime=None):
        assert runtime is worker_runtime
        calls.append((directory, backup_existing))
        if directory == first.parent:
            return True, 'deployed', directory / 'Version.dll'
        return False, 'blocked', None

    monkeypatch.setattr(krkrz_module, 'deploy_krkrz_patch', deploy)
    worker = KrkrzWorker(
        [first, second],
        'deploy',
        backup_existing=False,
        runtime=worker_runtime,
    )
    completed: list[tuple[str, bool, str]] = []
    totals: list[tuple[int, int]] = []
    worker.one_done.connect(
        lambda path, ok, message: completed.append((path, ok, message))
    )
    worker.all_done.connect(lambda success, fail: totals.append((success, fail)))

    worker.run()

    assert calls == [(first.parent, False), (second.parent, False)]
    assert completed == [
        (str(first), True, 'deployed'),
        (str(second), False, 'blocked'),
    ]
    assert totals == [(1, 1)]


def test_krkrz_worker_converts_domain_exception_and_continues(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / 'one' / 'game.exe'
    second = tmp_path / 'two' / 'game.exe'

    def deploy(directory: Path, *, backup_existing: bool, runtime=None):
        del backup_existing, runtime
        if directory == first.parent:
            raise OSError('deployment unavailable')
        return True, 'deployed', directory / 'Version.dll'

    monkeypatch.setattr(krkrz_module, 'deploy_krkrz_patch', deploy)
    worker = KrkrzWorker([first, second], 'deploy')
    lines: list[str] = []
    completed: list[tuple[str, bool, str]] = []
    totals: list[tuple[int, int]] = []
    worker.line.connect(lines.append)
    worker.one_done.connect(
        lambda path, ok, message: completed.append((path, ok, message))
    )
    worker.all_done.connect(lambda success, fail: totals.append((success, fail)))

    worker.run()

    assert completed[0][0] == str(first)
    assert completed[0][1] is False
    assert 'OSError: deployment unavailable' in completed[0][2]
    assert completed[1] == (str(second), True, 'deployed')
    assert any(line.startswith('[fail] 未預期錯誤') for line in lines)
    assert totals == [(1, 1)]


def test_krkrz_worker_emits_totals_even_when_fatal_error_escapes(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del qtbot

    class SimulatedFatalError(BaseException):
        pass

    def crash(*_args, **_kwargs):
        raise SimulatedFatalError('simulated thread abort')

    monkeypatch.setattr(krkrz_module, 'deploy_krkrz_patch', crash)
    worker = KrkrzWorker([tmp_path / 'game.exe'], 'deploy')
    totals: list[tuple[int, int]] = []
    worker.all_done.connect(lambda success, fail: totals.append((success, fail)))

    with pytest.raises(SimulatedFatalError, match='simulated thread abort'):
        worker.run()

    assert totals == [(0, 0)]


def test_krkrz_steamless_preprocess_skips_when_tool_is_missing(
    qtbot: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        krkrz_module,
        'is_steamless_installed',
        lambda *, runtime=None: False,
    )
    worker = KrkrzWorker([tmp_path / 'game.exe'], 'deploy')
    lines: list[str] = []
    worker.line.connect(lines.append)

    worker._steamless_pre_unpack(tmp_path / 'game.exe')

    assert lines == ['[steamless] 未安裝，跳過預處理']
