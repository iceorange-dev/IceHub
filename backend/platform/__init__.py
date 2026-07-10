"""Operating-system integration used by the backend facade."""

from .files import _is_link_or_reparse, _validate_pe_file
from .steam import detect_steam_path, is_steam_running

__all__ = [
    '_is_link_or_reparse',
    '_validate_pe_file',
    'detect_steam_path',
    'is_steam_running',
]
