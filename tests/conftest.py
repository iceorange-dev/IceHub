"""Shared pytest setup for IceHub."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Keep imports identical on developer machines and in GitHub Actions without
# requiring an editable package install.
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Qt widgets can be constructed in CI without an interactive Windows desktop.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


@pytest.fixture(scope='session')
def project_root() -> Path:
    """Return the repository root without depending on the current directory."""
    return PROJECT_ROOT
