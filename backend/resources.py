"""Read-only bundled resource discovery across supported packagers."""

from __future__ import annotations

from pathlib import Path
import sys

from .runtime import BackendRuntime, resolve_runtime


def resource_path(
    rel: str,
    *,
    runtime: BackendRuntime | None = None,
) -> Path:
    """Locate a bundled resource without treating it as mutable app data."""

    active_runtime = resolve_runtime(runtime)
    resource_root = active_runtime.resource_root
    legacy_root = active_runtime.legacy_root
    candidates: list[Path] = []

    meipass = getattr(sys, '_MEIPASS', None)
    if meipass:
        candidates.append(Path(meipass) / rel)

    try:
        module_path = Path(__file__).resolve()
        candidates.append(module_path.parent.parent / rel)
        candidates.append(module_path.parent / rel)
    except Exception:
        pass

    candidates.append(resource_root / rel)
    candidates.append(legacy_root / rel)

    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except Exception:
            continue
    return candidates[0] if candidates else resource_root / rel


__all__ = ['resource_path']
