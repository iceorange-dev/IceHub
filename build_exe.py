#!/usr/bin/env python3
"""Build the Windows IceHub one-file executable with Nuitka.

Every invocation builds in a unique, empty directory.  The public artifact in
``dist`` is replaced only after Nuitka succeeds and the new file passes basic
freshness and PE checks, so a stale executable can never be reported as the
result of a failed build.
"""

from __future__ import annotations

import hashlib
import importlib.util
import multiprocessing
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from app.version import VERSION
from backend.constants import KRKRZ_BUNDLED_PAYLOAD_REL, KRKRZ_PAYLOAD_FILENAME


HERE = Path(__file__).resolve().parent
BUILD_ROOT = HERE / 'build'
DIST = HERE / 'dist'
ICON = HERE / 'assets' / 'icon.ico'
ASSETS = HERE / 'assets'
KRKRZ_DLL = ASSETS / 'krkrz' / 'Version.dll'
ENTRY = HERE / 'main.py'
OUTPUT_NAME = 'IceHub.exe'
WINDOWS_VERSION = f'{VERSION}.0'

REQUIRED_MODULES = {
    'nuitka': 'Nuitka (install requirements-dev.txt)',
    'PyQt6': 'PyQt6',
    'qfluentwidgets': 'PyQt6-Fluent-Widgets',
    'httpx': 'httpx',
    'psutil': 'psutil',
    'packaging': 'packaging',
}


def _check_prerequisites() -> bool:
    missing_paths = [path for path in (ENTRY, ASSETS, KRKRZ_DLL) if not path.exists()]
    if missing_paths:
        for path in missing_paths:
            print(f'Missing required build input: {path}', file=sys.stderr)
        return False

    missing_modules = [label for module, label in REQUIRED_MODULES.items() if importlib.util.find_spec(module) is None]
    if missing_modules:
        print('Missing build dependencies: ' + ', '.join(missing_modules), file=sys.stderr)
        return False
    return True


def _is_fresh_pe(path: Path, build_started_at: float) -> bool:
    """Verify that *path* is a non-empty PE created by this build invocation."""

    try:
        stat = path.stat()
        # A unique output directory already excludes stale files.  The timestamp
        # check catches unexpected tool behaviour while allowing coarse filesystems.
        if not path.is_file() or stat.st_size < 1024 or stat.st_mtime < build_started_at - 2:
            return False
        with path.open('rb') as executable:
            return executable.read(2) == b'MZ'
    except OSError:
        return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _publish(candidate: Path) -> Path:
    """Atomically replace the public executable with a verified candidate."""

    DIST.mkdir(parents=True, exist_ok=True)
    destination = DIST / OUTPUT_NAME
    temporary = DIST / f'.{OUTPUT_NAME}.{uuid.uuid4().hex}.tmp'
    try:
        shutil.copy2(candidate, temporary)
        if temporary.stat().st_size != candidate.stat().st_size or _sha256(temporary) != _sha256(candidate):
            raise OSError('published executable did not match the build output')
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _stage_krkrz_payload(staging_dir: Path) -> Path:
    """Copy the reviewed DLL bytes to a Nuitka-safe data-file extension."""

    staging_dir.mkdir(parents=True, exist_ok=True)
    payload = staging_dir / KRKRZ_PAYLOAD_FILENAME
    temporary = staging_dir / f'.{KRKRZ_PAYLOAD_FILENAME}.{uuid.uuid4().hex}.tmp'
    try:
        shutil.copy2(KRKRZ_DLL, temporary)
        if (
            temporary.stat().st_size != KRKRZ_DLL.stat().st_size
            or _sha256(temporary) != _sha256(KRKRZ_DLL)
        ):
            raise OSError('staged KRKRZ payload did not match Version.dll')
        with temporary.open('rb') as stream:
            if stream.read(2) != b'MZ':
                raise OSError('KRKRZ Version.dll is not a PE payload')
        os.replace(temporary, payload)
        return payload
    finally:
        temporary.unlink(missing_ok=True)


def _build_arguments(output_dir: Path, krkrz_payload: Path) -> list[str]:
    jobs = min(max(multiprocessing.cpu_count(), 1), 8)
    args = [
        sys.executable,
        '-m',
        'nuitka',
        str(ENTRY),
        '--mode=onefile',
        '--enable-plugin=pyqt6',
        '--windows-console-mode=disable',
        f'--include-data-dir={ASSETS}=assets',
        (
            f'--include-data-files={krkrz_payload}='
            f'{KRKRZ_BUNDLED_PAYLOAD_REL}'
        ),
        # qfluentwidgets loads theme resources dynamically, so keep the explicit
        # package and data includes.  Project packages are statically imported.
        '--include-package=qfluentwidgets',
        '--include-package-data=qfluentwidgets',
        '--lto=no',
        f'--jobs={jobs}',
        f'--output-dir={output_dir}',
        f'--output-filename={OUTPUT_NAME}',
        '--remove-output',
        '--assume-yes-for-downloads',
        '--company-name=IceOrange',
        '--product-name=IceHub',
        f'--file-version={WINDOWS_VERSION}',
        f'--product-version={WINDOWS_VERSION}',
        '--file-description=IceHub - Steam library tool (OST / Steamless / KRKRZ)',
        '--copyright=Copyright (c) IceOrange. Licensed under GPL-3.0-only.',
    ]
    if ICON.exists():
        args.append(f'--windows-icon-from-ico={ICON}')
    return args


def build() -> bool:
    if sys.platform != 'win32':
        print('IceHub can only be packaged on Windows.', file=sys.stderr)
        return False
    if not _check_prerequisites():
        return False

    BUILD_ROOT.mkdir(parents=True, exist_ok=True)
    invocation_dir = BUILD_ROOT / f'nuitka-{uuid.uuid4().hex}'
    invocation_dir.mkdir()
    try:
        output_dir = invocation_dir / 'output'
        output_dir.mkdir()
        krkrz_payload = _stage_krkrz_payload(invocation_dir / 'payload')
        args = _build_arguments(output_dir, krkrz_payload)

        print('Building IceHub.exe with Nuitka:')
        for argument in args[3:]:
            print('  ', argument)

        started_wall = time.time()
        started_monotonic = time.monotonic()
        result = subprocess.run(args, cwd=HERE, check=False)
        duration = time.monotonic() - started_monotonic
        candidate = output_dir / OUTPUT_NAME

        if result.returncode != 0:
            print(f'Build failed after {duration:.0f}s (Nuitka exit {result.returncode}).', file=sys.stderr)
            return False
        if not _is_fresh_pe(candidate, started_wall):
            print(f'Build failed: Nuitka did not produce a fresh, valid PE at {candidate}.', file=sys.stderr)
            return False

        try:
            executable = _publish(candidate)
        except OSError as error:
            print(f'Build succeeded but publishing failed: {error}', file=sys.stderr)
            return False

        size_mb = executable.stat().st_size / (1024 * 1024)
        digest = _sha256(executable)
        print(f'Done in {duration:.0f}s -> {executable} ({size_mb:.1f} MB)')
        print(f'SHA256: {digest}')
        return True
    except (OSError, subprocess.SubprocessError) as error:
        print(f'Unable to stage resources or run Nuitka: {error}', file=sys.stderr)
        return False
    finally:
        try:
            shutil.rmtree(invocation_dir)
        except OSError as error:
            print(f'Warning: unable to remove temporary build directory {invocation_dir}: {error}', file=sys.stderr)


if __name__ == '__main__':
    raise SystemExit(0 if build() else 1)
