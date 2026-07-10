"""Tests for the executable entry point and its bundle diagnostic mode."""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

import backend.installers.krkrz as krkrz
import main as application_entrypoint


@pytest.fixture(autouse=True)
def disable_bootstrap_log_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        application_entrypoint,
        '_configure_bootstrap_logging',
        lambda: None,
    )


def test_verify_bundle_exits_before_application_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = ModuleType('app.bootstrap')

    def unexpected_application_start() -> int:
        pytest.fail('bundle verification must not start QApplication')

    bootstrap.main = unexpected_application_start  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'app.bootstrap', bootstrap)
    monkeypatch.setattr(
        application_entrypoint,
        '_verify_bundled_krkrz_resource',
        lambda: 0,
    )

    assert application_entrypoint.main(argv=('--verify-bundle',)) == 0


def test_verify_bundle_reports_missing_resource(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(krkrz, 'bundled_krkrz_dll', lambda: None)

    assert application_entrypoint.main(argv=('--verify-bundle',)) == 1
    assert 'Version.dll was not found' in capsys.readouterr().err


def test_verify_bundle_rejects_invalid_pe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = tmp_path / 'Version.dll'
    invalid.write_bytes(b'not a Windows PE' * 8)
    monkeypatch.setattr(krkrz, 'bundled_krkrz_dll', lambda: invalid)

    assert application_entrypoint.main(argv=('--verify-bundle',)) == 1
    output = capsys.readouterr()
    assert 'Bundle verification failed' in output.err
    assert str(invalid) not in output.out


def test_source_tree_bundle_passes_same_diagnostic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert application_entrypoint.main(argv=('--verify-bundle',)) == 0
    output = capsys.readouterr()
    assert 'Bundle verification succeeded' in output.out
    assert 'Version.dll' in output.out


def test_normal_startup_still_delegates_to_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = ModuleType('app.bootstrap')
    bootstrap.main = lambda: 17  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'app.bootstrap', bootstrap)

    assert application_entrypoint.main(argv=()) == 17
