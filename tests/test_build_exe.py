"""Focused tests for reproducible Nuitka build inputs."""

from __future__ import annotations

from pathlib import Path

import build_exe
import pytest
from app.version import VERSION
from backend.constants import KRKRZ_BUNDLED_PAYLOAD_REL


def test_krkrz_payload_is_staged_as_verified_bin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / 'Version.dll'
    source.write_bytes(b'MZ' + (b'\0' * 128) + b'KRKRZ')
    monkeypatch.setattr(build_exe, 'KRKRZ_DLL', source)

    payload = build_exe._stage_krkrz_payload(tmp_path / 'staging')

    assert payload.name == 'Version.bin'
    assert payload.read_bytes() == source.read_bytes()
    assert build_exe._sha256(payload) == build_exe._sha256(source)


def test_krkrz_staging_rejects_a_non_pe_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / 'Version.dll'
    source.write_bytes(b'not a PE payload')
    monkeypatch.setattr(build_exe, 'KRKRZ_DLL', source)
    staging = tmp_path / 'staging'

    with pytest.raises(OSError, match='not a PE payload'):
        build_exe._stage_krkrz_payload(staging)

    assert not (staging / 'Version.bin').exists()


def test_build_arguments_explicitly_include_staged_krkrz_payload(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / 'output'
    payload = tmp_path / 'payload' / 'Version.bin'

    arguments = build_exe._build_arguments(output_dir, payload)

    assert (
        f'--include-data-files={payload}={KRKRZ_BUNDLED_PAYLOAD_REL}'
        in arguments
    )
    assert f'--output-dir={output_dir}' in arguments
    assert f'--file-version={VERSION}.0' in arguments
    assert f'--product-version={VERSION}.0' in arguments
