"""Focused tests for extracted operating-system integration helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

import backend.ost_backend as backend_facade
from backend.platform import files, steam
from backend.security import SecurityValidationError


def test_pe_preflight_accepts_pe_shape_and_rejects_non_pe(tmp_path: Path) -> None:
    valid = tmp_path / 'valid.dll'
    valid.write_bytes(b'MZ' + b'\0' * 62)
    invalid = tmp_path / 'invalid.dll'
    invalid.write_bytes(b'NO' + b'\0' * 62)

    files._validate_pe_file(valid)
    with pytest.raises(SecurityValidationError):
        files._validate_pe_file(invalid)


def test_regular_file_is_not_a_link_or_reparse_point(tmp_path: Path) -> None:
    regular = tmp_path / 'regular.bin'
    regular.write_bytes(b'data')

    assert files._is_link_or_reparse(regular) is False


def test_custom_steam_path_requires_steam_executable(tmp_path: Path) -> None:
    steam_root = tmp_path / 'Steam'
    steam_root.mkdir()
    (steam_root / 'steam.exe').write_bytes(b'fixture')

    assert steam.detect_steam_path(str(steam_root)) == steam_root.resolve()


def test_steam_process_detection_uses_process_name(monkeypatch: pytest.MonkeyPatch) -> None:
    class Process:
        info = {'name': 'Steam.EXE'}

    monkeypatch.setattr(steam.psutil, 'process_iter', lambda _fields: [Process()])

    assert steam.is_steam_running() is True


def test_backend_facade_statically_reexports_platform_helpers() -> None:
    assert backend_facade.detect_steam_path is steam.detect_steam_path
    assert backend_facade.is_steam_running is steam.is_steam_running
    assert backend_facade._validate_pe_file is files._validate_pe_file
    assert backend_facade._is_link_or_reparse is files._is_link_or_reparse
