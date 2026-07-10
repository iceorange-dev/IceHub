"""Windows-aware file preflight helpers shared by deployment domains."""

from __future__ import annotations

from pathlib import Path
import stat

from ..security import SecurityValidationError


def _is_link_or_reparse(path: Path) -> bool:
    """Return whether *path* is a symbolic link or Windows reparse point."""

    path = Path(path)
    if path.is_symlink():
        return True
    try:
        attributes = getattr(path.lstat(), 'st_file_attributes', 0)
    except OSError:
        return False
    return bool(
        attributes
        & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    )


def _validate_pe_file(
    path: Path,
    *,
    max_size: int = 128 * 1024 * 1024,
) -> None:
    """Reject files that cannot safely be treated as Windows PE binaries."""

    path = Path(path)
    size = path.stat().st_size
    if size < 64 or size > max_size:
        raise SecurityValidationError(
            f'PE 檔案大小不合理: {path.name} ({size} bytes)'
        )
    with path.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise SecurityValidationError(
                f'檔案不是有效的 Windows PE: {path.name}'
            )


__all__ = ['_is_link_or_reparse', '_validate_pe_file']
