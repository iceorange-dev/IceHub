"""Steam installation and process discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psutil

try:
    import winreg
except ImportError:  # pragma: no cover - the desktop application targets Windows
    winreg: Any = None


def _valid_steam_path(path: Path) -> Path | None:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_dir() or not (resolved / 'steam.exe').is_file():
        return None
    return resolved


def detect_steam_path(custom: str = '') -> Path | None:
    """Resolve a custom Steam directory, then fall back to the registry."""

    if custom:
        valid = _valid_steam_path(Path(custom))
        if valid:
            return valid
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam') as key:
            steam_path, _ = winreg.QueryValueEx(key, 'SteamPath')
        return _valid_steam_path(Path(steam_path))
    except Exception:
        return None


def is_steam_running() -> bool:
    """Return whether a visible process is named ``steam.exe``."""

    try:
        for process in psutil.process_iter(['name']):
            try:
                if (process.info.get('name') or '').lower() == 'steam.exe':
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False


__all__ = ['detect_steam_path', 'is_steam_running']
