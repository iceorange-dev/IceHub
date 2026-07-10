"""Security primitives for validating remote data and extracting ZIP files.

The helpers in this module deliberately use a small, explicit grammar.  Remote
data should be rejected when it is ambiguous rather than handed to Lua or the
filesystem and interpreted there.
"""

from __future__ import annotations

from collections.abc import Collection
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import BinaryIO
import unicodedata
import zipfile


class SecurityValidationError(ValueError):
    """Raised when untrusted input does not satisfy the security policy."""


class UnsafeArchiveError(SecurityValidationError):
    """Raised when a ZIP archive is unsafe or exceeds extraction limits."""


_DEPOT_KEY_RE = re.compile(r"[0-9A-Fa-f]{64}\Z", re.ASCII)
_DECIMAL_RE = re.compile(r"[0-9]+\Z", re.ASCII)
_GITHUB_OWNER_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}\Z",
    re.ASCII,
)
_GITHUB_REPO_RE = re.compile(r"[A-Za-z0-9._-]{1,100}\Z", re.ASCII)
_WINDOWS_DRIVE_RE = re.compile(r"[A-Za-z]:", re.ASCII)
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}

_ADD_APP_RE = re.compile(
    r"addappid[ \t]*\([ \t]*(?P<appid>[0-9]+)[ \t]*\)",
    re.ASCII,
)
_ADD_APP_WITH_KEY_RE = re.compile(
    r'addappid[ \t]*\([ \t]*(?P<appid>[0-9]+)[ \t]*,[ \t]*1[ \t]*,'
    r'[ \t]*"(?P<key>[0-9A-Fa-f]{64})"[ \t]*\)',
    re.ASCII,
)
_SET_MANIFEST_RE = re.compile(
    r'setManifestid[ \t]*\([ \t]*(?P<depot_id>[0-9]+)[ \t]*,'
    r'[ \t]*"(?P<gid>[0-9]+)"[ \t]*\)',
    re.ASCII,
)


def _reject_control_characters(
    value: str,
    *,
    field: str,
    allowed: str = "\t\r\n",
) -> None:
    for character in value:
        if character in allowed:
            continue
        # Cc contains conventional controls; Cf contains invisible formatting
        # controls (including bidi overrides); Cs contains lone surrogates.
        if unicodedata.category(character) in {"Cc", "Cf", "Cs"}:
            raise SecurityValidationError(
                f"{field} contains a forbidden control character"
            )


def validate_depot_key(value: str) -> str:
    """Validate and return a canonical lowercase 64-character depot key."""

    if not isinstance(value, str) or _DEPOT_KEY_RE.fullmatch(value) is None:
        raise SecurityValidationError("depot key must contain exactly 64 hex digits")
    return value.lower()


def validate_manifest_gid(value: str | int) -> str:
    """Validate a decimal unsigned 64-bit manifest GID and return it as text."""

    if isinstance(value, bool):
        raise SecurityValidationError("manifest GID must be a decimal uint64")
    text = str(value) if isinstance(value, int) else value
    if not isinstance(text, str) or _DECIMAL_RE.fullmatch(text) is None:
        raise SecurityValidationError("manifest GID must be a decimal uint64")
    canonical = text.lstrip("0") or "0"
    uint64_max = "18446744073709551615"
    if len(canonical) > len(uint64_max) or (
        len(canonical) == len(uint64_max) and canonical > uint64_max
    ):
        raise SecurityValidationError("manifest GID is outside the uint64 range")
    return canonical


def _parse_github_repo(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or value.count("/") != 1:
        raise SecurityValidationError("GitHub repository must use owner/repo format")
    owner, repository = value.split("/", 1)
    if _GITHUB_OWNER_RE.fullmatch(owner) is None:
        raise SecurityValidationError("invalid GitHub repository owner")
    if (
        _GITHUB_REPO_RE.fullmatch(repository) is None
        or repository in {".", ".."}
        or repository.endswith(".")
    ):
        raise SecurityValidationError("invalid GitHub repository name")
    return owner, repository


def validate_github_repo(
    value: str,
    *,
    allowlist: Collection[str] | None = None,
) -> str:
    """Validate ``owner/repo`` and optionally require an exact allowlist match.

    GitHub repository identifiers and allowlist comparisons are
    case-insensitive.  The validated identifier is returned with the input's
    original casing so it remains suitable for logs and API URLs.
    """

    owner, repository = _parse_github_repo(value)
    validated = f"{owner}/{repository}"
    if allowlist is not None:
        if isinstance(allowlist, (str, bytes)):
            raise TypeError("allowlist must be a collection of repository names")
        allowed: set[str] = set()
        for candidate in allowlist:
            allowed_owner, allowed_repository = _parse_github_repo(candidate)
            allowed.add(f"{allowed_owner}/{allowed_repository}".casefold())
        if validated.casefold() not in allowed:
            raise SecurityValidationError("GitHub repository is not allowlisted")
    return validated


def validate_remote_lua(source: str, *, expected_appid: str | int | None = None) -> str:
    """Validate Lua using the minimal OpenSteamTool data-file grammar.

    Accepted lines are blank lines, ``--`` comments, and exactly one of::

        addappid(123)
        addappid(123, 1, "<64 hex digits>")
        setManifestid(123, "456")

    Whitespace and a trailing ``--`` comment are permitted.  No general Lua
    syntax is accepted.  When ``expected_appid`` is supplied, that app ID must
    occur as the first argument of an ``addappid`` call.
    """

    if not isinstance(source, str):
        raise SecurityValidationError("Lua source must be text")
    _reject_control_characters(source, field="Lua source")
    # Commands in the accepted data grammar never need square brackets.  Lua
    # long comments/strings do, and allowing them makes a visually commented
    # suffix executable after a later ``]]`` delimiter (for example
    # ``--[[ ... --]] os.execute(...)``).  Rejecting both delimiters keeps line
    # comments unambiguously line-scoped without implementing a Lua parser.
    if "[" in source or "]" in source:
        raise SecurityValidationError("Lua long comments and strings are not allowed")

    expected: str | None = None
    if expected_appid is not None:
        if isinstance(expected_appid, bool):
            raise SecurityValidationError("expected AppID must contain only digits")
        expected_text = (
            str(expected_appid) if isinstance(expected_appid, int) else expected_appid
        )
        if (
            not isinstance(expected_text, str)
            or _DECIMAL_RE.fullmatch(expected_text) is None
        ):
            raise SecurityValidationError("expected AppID must contain only digits")
        expected = expected_text.lstrip("0") or "0"

    seen_appids: set[str] = set()
    normalized_newlines = source.replace("\r\n", "\n").replace("\r", "\n")
    for line_number, line in enumerate(normalized_newlines.split("\n"), start=1):
        stripped = line.strip(" \t")
        if not stripped or stripped.startswith("--"):
            continue

        # A Lua line comment is safe after a validated command.  Split only at
        # the first marker; the command grammar itself never contains a '-'.
        command = line.split("--", 1)[0].strip(" \t")
        match = _ADD_APP_RE.fullmatch(command)
        if match is not None:
            seen_appids.add(match.group("appid").lstrip("0") or "0")
            continue

        match = _ADD_APP_WITH_KEY_RE.fullmatch(command)
        if match is not None:
            validate_depot_key(match.group("key"))
            seen_appids.add(match.group("appid").lstrip("0") or "0")
            continue

        match = _SET_MANIFEST_RE.fullmatch(command)
        if match is not None:
            validate_manifest_gid(match.group("gid"))
            continue

        raise SecurityValidationError(f"unsupported Lua syntax on line {line_number}")

    if expected is not None and expected not in seen_appids:
        raise SecurityValidationError(
            f"Lua source does not contain addappid({expected})"
        )
    return source


def _zip_info_is_link_or_special(member: zipfile.ZipInfo) -> bool:
    unix_mode = (member.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type == stat.S_IFLNK:
        return True
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        return True
    # Preserve the check for ZIPs made on Windows that carry a reparse-point
    # attribute rather than a Unix symlink mode.
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(member.external_attr & reparse_attribute)


def _member_name(member: str | zipfile.ZipInfo) -> str:
    if isinstance(member, zipfile.ZipInfo):
        if _zip_info_is_link_or_special(member):
            raise UnsafeArchiveError(f"ZIP member is a link or special file: {member.filename!r}")
        name = member.filename
    elif isinstance(member, str):
        name = member
    else:
        raise TypeError("ZIP member must be a name or ZipInfo")
    return name


def normalize_zip_member(member: str | zipfile.ZipInfo) -> PurePosixPath:
    """Return a safe, platform-neutral relative path for a ZIP member."""

    name = _member_name(member)
    if not name or "\x00" in name:
        raise UnsafeArchiveError("ZIP member name is empty or contains NUL")
    try:
        _reject_control_characters(name, field="ZIP member name", allowed="")
    except SecurityValidationError as error:
        raise UnsafeArchiveError(str(error)) from error

    portable_name = name.replace("\\", "/")
    if portable_name.startswith("/") or _WINDOWS_DRIVE_RE.match(portable_name):
        raise UnsafeArchiveError(f"ZIP member path is absolute: {name!r}")

    raw_parts = portable_name.split("/")
    if ".." in raw_parts:
        raise UnsafeArchiveError(f"ZIP member contains parent traversal: {name!r}")

    parts: list[str] = []
    for part in raw_parts:
        if part in {"", "."}:
            continue
        # Colons create drive-relative paths or NTFS alternate data streams.
        if ":" in part:
            raise UnsafeArchiveError(f"ZIP member contains a forbidden colon: {name!r}")
        if part.endswith((" ", ".")):
            raise UnsafeArchiveError(
                f"ZIP member has a Windows-ambiguous component: {name!r}"
            )
        device_name = part.split(".", 1)[0].upper()
        if device_name in _WINDOWS_RESERVED_NAMES:
            raise UnsafeArchiveError(
                f"ZIP member uses a reserved Windows name: {name!r}"
            )
        parts.append(part)

    if not parts:
        raise UnsafeArchiveError(f"ZIP member has no usable path: {name!r}")
    return PurePosixPath(*parts)


def _is_reparse_point(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return False
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_attribute)


def safe_zip_destination(
    destination: str | os.PathLike[str],
    member: str | zipfile.ZipInfo,
) -> Path:
    """Resolve a ZIP member below ``destination`` and enforce containment."""

    base_input = Path(destination)
    if _is_reparse_point(base_input):
        raise UnsafeArchiveError("ZIP destination must not be a link or reparse point")
    base = base_input.resolve(strict=False)
    normalized = normalize_zip_member(member)
    target = (base / Path(*normalized.parts)).resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError as error:
        raise UnsafeArchiveError("ZIP member escapes the extraction directory") from error

    # Existing links inside the output tree could redirect a later open even
    # when lexical normalization succeeded.
    current = base
    for part in normalized.parts:
        current /= part
        if _is_reparse_point(current):
            raise UnsafeArchiveError(
                f"ZIP destination contains a link or reparse point: {current}"
            )
    return target


def _zip_member_is_directory(member: zipfile.ZipInfo) -> bool:
    unix_mode = (member.external_attr >> 16) & 0xFFFF
    return (
        member.is_dir()
        or member.filename.endswith(("/", "\\"))
        or stat.S_IFMT(unix_mode) == stat.S_IFDIR
    )


def _validate_limit(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")


def safe_extract_zip(
    archive: str | os.PathLike[str] | BinaryIO | zipfile.ZipFile,
    destination: str | os.PathLike[str],
    *,
    max_files: int = 1_000,
    max_file_size: int = 512 * 1024 * 1024,
    max_total_size: int = 2 * 1024 * 1024 * 1024,
    chunk_size: int = 1024 * 1024,
    overwrite: bool = False,
) -> list[Path]:
    """Safely extract a ZIP archive and return extracted file paths.

    Every central-directory entry is checked before the destination directory
    is created.  Limits apply to uncompressed sizes.  Links, special files,
    duplicate destinations, encrypted entries, traversal, and pre-existing
    output files are rejected by default.
    """

    _validate_limit(max_files, "max_files")
    _validate_limit(max_file_size, "max_file_size")
    _validate_limit(max_total_size, "max_total_size")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")

    owns_archive = not isinstance(archive, zipfile.ZipFile)
    zip_file = zipfile.ZipFile(archive, "r") if owns_archive else archive
    destination_path = Path(destination)
    try:
        members = zip_file.infolist()
        if len(members) > max_files:
            raise UnsafeArchiveError(
                f"ZIP has {len(members)} members; limit is {max_files}"
            )

        planned: list[tuple[zipfile.ZipInfo, Path, bool]] = []
        seen_targets: set[str] = set()
        total_size = 0
        for member in members:
            if member.flag_bits & 0x1:
                raise UnsafeArchiveError(
                    f"encrypted ZIP members are not accepted: {member.filename!r}"
                )
            is_directory = _zip_member_is_directory(member)
            if member.file_size < 0:
                raise UnsafeArchiveError("ZIP member has a negative size")
            if is_directory and member.file_size:
                raise UnsafeArchiveError(
                    f"ZIP directory has file data: {member.filename!r}"
                )
            if not is_directory and member.file_size > max_file_size:
                raise UnsafeArchiveError(
                    f"ZIP member exceeds the per-file limit: {member.filename!r}"
                )
            total_size += member.file_size
            if total_size > max_total_size:
                raise UnsafeArchiveError("ZIP exceeds the total uncompressed-size limit")

            target = safe_zip_destination(destination_path, member)
            target_key = os.path.normcase(str(target))
            if target_key in seen_targets:
                raise UnsafeArchiveError(
                    f"ZIP has duplicate output paths: {member.filename!r}"
                )
            seen_targets.add(target_key)
            planned.append((member, target, is_directory))

        # A file cannot also be the parent directory of another archive entry.
        file_targets = {
            os.path.normcase(str(target))
            for _, target, is_directory in planned
            if not is_directory
        }
        for _, target, _ in planned:
            for parent in target.parents:
                if parent == destination_path.resolve(strict=False):
                    break
                if os.path.normcase(str(parent)) in file_targets:
                    raise UnsafeArchiveError("ZIP contains a file/directory path conflict")

        for _, target, is_directory in planned:
            if _is_reparse_point(target):
                raise UnsafeArchiveError(f"output path is a link or reparse point: {target}")
            if not is_directory and target.exists() and not overwrite:
                raise UnsafeArchiveError(f"output file already exists: {target}")

        destination_path.mkdir(parents=True, exist_ok=True)
        extracted: list[Path] = []
        actual_total = 0
        for member, target, is_directory in planned:
            if is_directory:
                target.mkdir(parents=True, exist_ok=True)
                continue

            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            mode = "wb" if overwrite else "xb"
            try:
                with zip_file.open(member, "r") as source, target.open(mode) as output:
                    while True:
                        chunk = source.read(chunk_size)
                        if not chunk:
                            break
                        written += len(chunk)
                        actual_total += len(chunk)
                        if written > max_file_size or actual_total > max_total_size:
                            raise UnsafeArchiveError(
                                "ZIP expanded beyond the configured extraction limits"
                            )
                        output.write(chunk)
            except Exception:
                # Do not leave a newly-created partial executable or DLL behind.
                if target.exists() and not overwrite:
                    target.unlink()
                raise
            if written != member.file_size:
                if target.exists() and not overwrite:
                    target.unlink()
                raise UnsafeArchiveError(
                    f"ZIP member size changed during extraction: {member.filename!r}"
                )
            extracted.append(target)
        return extracted
    finally:
        if owns_archive:
            zip_file.close()


def sha256_file(path: str | os.PathLike[str], *, chunk_size: int = 1024 * 1024) -> str:
    """Return the lowercase SHA-256 hex digest of a file."""

    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "SecurityValidationError",
    "UnsafeArchiveError",
    "normalize_zip_member",
    "safe_extract_zip",
    "safe_zip_destination",
    "sha256_file",
    "validate_depot_key",
    "validate_github_repo",
    "validate_manifest_gid",
    "validate_remote_lua",
]
