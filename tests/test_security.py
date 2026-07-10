from __future__ import annotations

import hashlib
import io
from pathlib import Path, PurePosixPath
import stat
import zipfile

import pytest

from backend.security import (
    SecurityValidationError,
    UnsafeArchiveError,
    normalize_zip_member,
    safe_extract_zip,
    safe_zip_destination,
    sha256_file,
    validate_depot_key,
    validate_github_repo,
    validate_manifest_gid,
    validate_remote_lua,
)


VALID_KEY = "0123456789abcdef" * 4


def make_zip(entries: list[tuple[str | zipfile.ZipInfo, bytes]]) -> io.BytesIO:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    stream.seek(0)
    return stream


def test_validate_remote_lua_accepts_only_supported_data_commands() -> None:
    source = (
        "-- generated data\r\n"
        "addappid(480) -- base game\r\n"
        f'\taddappid(481, 1, "{VALID_KEY.upper()}")\r\n'
        'setManifestid(481, "18446744073709551615")\r\n'
    )

    assert validate_remote_lua(source, expected_appid="0480") == source


@pytest.mark.parametrize(
    "source",
    [
        'os.execute("calc")',
        'require("socket")',
        "addappid(480); os.execute('calc')",
        "addappid(480) addappid(481)",
        "if true then addappid(480) end",
        "addappid(-1)",
        f'addappid(481, 2, "{VALID_KEY}")',
        f'addappid(481, 1, "{VALID_KEY}00")',
        'setManifestid(481, "12x")',
        'setManifestid(481, "18446744073709551616")',
        "--[[\nos.execute('calc')\n]]",
    ],
)
def test_validate_remote_lua_rejects_injection_and_invalid_arguments(source: str) -> None:
    with pytest.raises(SecurityValidationError):
        validate_remote_lua(source)


@pytest.mark.parametrize("control", ["\x00", "\x1b", "\x7f", "\u202e"])
def test_validate_remote_lua_rejects_control_and_format_characters(control: str) -> None:
    with pytest.raises(SecurityValidationError, match="control character"):
        validate_remote_lua(f"addappid(480){control}")


def test_validate_remote_lua_requires_expected_appid_in_addappid() -> None:
    source = 'addappid(481)\nsetManifestid(480, "123")\n'

    with pytest.raises(SecurityValidationError, match=r"addappid\(480\)"):
        validate_remote_lua(source, expected_appid=480)


def test_validate_remote_lua_rejects_long_comment_termination_bypass() -> None:
    source = '--[[\naddappid(480)\n--]] os.execute("calc")\n'

    with pytest.raises(SecurityValidationError, match='long comments'):
        validate_remote_lua(source, expected_appid=480)


def test_validate_remote_lua_handles_unbounded_digit_input_without_int_conversion() -> None:
    huge_appid = "0" * 5_000 + "480"

    assert validate_remote_lua(
        f"addappid({huge_appid})", expected_appid="480"
    ) == f"addappid({huge_appid})"


def test_depot_key_validation_is_exact_and_canonical() -> None:
    assert validate_depot_key(VALID_KEY.upper()) == VALID_KEY
    for invalid in (VALID_KEY[:-1], VALID_KEY + "0", "g" * 64, f" {VALID_KEY}"):
        with pytest.raises(SecurityValidationError):
            validate_depot_key(invalid)


@pytest.mark.parametrize(
    ("value", "canonical"),
    [(0, "0"), ("00042", "42"), ("18446744073709551615", "18446744073709551615")],
)
def test_manifest_gid_accepts_decimal_uint64(value: str | int, canonical: str) -> None:
    assert validate_manifest_gid(value) == canonical


@pytest.mark.parametrize(
    "value",
    [-1, True, "-1", "+1", "1.0", " 1", "18446744073709551616"],
)
def test_manifest_gid_rejects_non_decimal_or_out_of_range(value: object) -> None:
    with pytest.raises(SecurityValidationError):
        validate_manifest_gid(value)  # type: ignore[arg-type]


def test_manifest_gid_rejects_huge_decimal_without_int_conversion() -> None:
    with pytest.raises(SecurityValidationError, match="uint64 range"):
        validate_manifest_gid("9" * 5_000)


def test_github_repo_format_and_allowlist() -> None:
    assert (
        validate_github_repo("OpenAI-Ice/Ice_Hub", allowlist={"openai-ice/ice_hub"})
        == "OpenAI-Ice/Ice_Hub"
    )
    for invalid in (
        "owner",
        "owner/repo/extra",
        "-owner/repo",
        "owner-/repo",
        "owner/repo name",
        "owner/..",
        "owner/repo.",
        "https://github.com/owner/repo",
    ):
        with pytest.raises(SecurityValidationError):
            validate_github_repo(invalid)
    with pytest.raises(SecurityValidationError, match="allowlisted"):
        validate_github_repo("owner/repo", allowlist={"other/repo"})


@pytest.mark.parametrize(
    "name",
    [
        "/absolute/file.dll",
        r"\absolute\file.dll",
        r"C:\temp\file.dll",
        "C:relative.dll",
        "../escape.dll",
        "tools/../../escape.dll",
        r"tools\..\escape.dll",
        "tools/file.dll:stream",
        "tools/NUL.txt",
        "tools/name. ",
        "tools/line\nbreak.dll",
        "tools/tab\tname.dll",
    ],
)
def test_normalize_zip_member_rejects_unsafe_paths(name: str) -> None:
    with pytest.raises(UnsafeArchiveError):
        normalize_zip_member(name)


def test_normalize_zip_member_canonicalizes_separators_and_dots() -> None:
    assert normalize_zip_member(r"tools\.\bin\app.exe") == PurePosixPath(
        "tools/bin/app.exe"
    )


def test_zip_info_symlink_is_rejected() -> None:
    member = zipfile.ZipInfo("link")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16

    with pytest.raises(UnsafeArchiveError, match="link or special"):
        normalize_zip_member(member)


def test_safe_zip_destination_stays_below_root(tmp_path: Path) -> None:
    target = safe_zip_destination(tmp_path, "tools/bin/app.exe")

    assert target == (tmp_path / "tools" / "bin" / "app.exe").resolve()


def test_safe_extract_zip_extracts_regular_files(tmp_path: Path) -> None:
    stream = make_zip(
        [
            ("tools/", b""),
            ("tools/readme.txt", b"hello"),
            (r"tools\bin\app.exe", b"binary"),
        ]
    )

    extracted = safe_extract_zip(stream, tmp_path / "output")

    assert {path.relative_to(tmp_path / "output").as_posix() for path in extracted} == {
        "tools/readme.txt",
        "tools/bin/app.exe",
    }
    assert (tmp_path / "output" / "tools" / "readme.txt").read_bytes() == b"hello"


def test_safe_extract_preflights_zip_slip_before_writing(tmp_path: Path) -> None:
    stream = make_zip([("safe.txt", b"safe"), ("../../escape.txt", b"owned")])
    output = tmp_path / "output"

    with pytest.raises(UnsafeArchiveError, match="traversal"):
        safe_extract_zip(stream, output)

    assert not output.exists()
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_rejects_symlink_entry(tmp_path: Path) -> None:
    link = zipfile.ZipInfo("link")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    stream = make_zip([(link, b"target")])

    with pytest.raises(UnsafeArchiveError, match="link or special"):
        safe_extract_zip(stream, tmp_path / "output")


def test_safe_extract_rejects_too_many_members(tmp_path: Path) -> None:
    stream = make_zip([("one", b"1"), ("two", b"2")])

    with pytest.raises(UnsafeArchiveError, match="members"):
        safe_extract_zip(stream, tmp_path / "output", max_files=1)


def test_safe_extract_rejects_large_single_file(tmp_path: Path) -> None:
    stream = make_zip([("bomb.bin", b"0" * 10_000)])

    with pytest.raises(UnsafeArchiveError, match="per-file"):
        safe_extract_zip(stream, tmp_path / "output", max_file_size=9_999)


def test_safe_extract_rejects_large_total_uncompressed_size(tmp_path: Path) -> None:
    stream = make_zip([("one.bin", b"0" * 6_000), ("two.bin", b"0" * 6_000)])

    with pytest.raises(UnsafeArchiveError, match="total uncompressed"):
        safe_extract_zip(
            stream,
            tmp_path / "output",
            max_file_size=10_000,
            max_total_size=10_000,
        )


def test_safe_extract_rejects_duplicate_normalized_destinations(tmp_path: Path) -> None:
    stream = make_zip([("tools/app.exe", b"one"), ("tools//app.exe", b"two")])

    with pytest.raises(UnsafeArchiveError, match="duplicate"):
        safe_extract_zip(stream, tmp_path / "output")


def test_safe_extract_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    existing = output / "app.exe"
    existing.write_bytes(b"original")
    stream = make_zip([("app.exe", b"replacement")])

    with pytest.raises(UnsafeArchiveError, match="already exists"):
        safe_extract_zip(stream, output)

    assert existing.read_bytes() == b"original"


def test_sha256_file_streams_expected_digest(tmp_path: Path) -> None:
    content = (b"IceHub\x00" * 10_000) + b"end"
    path = tmp_path / "artifact.bin"
    path.write_bytes(content)

    assert sha256_file(path, chunk_size=17) == hashlib.sha256(content).hexdigest()
