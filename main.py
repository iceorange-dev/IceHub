#!/usr/bin/env python3
"""IceHub executable entry point."""

from __future__ import annotations

from collections.abc import Sequence
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import threading
from types import TracebackType


LOG = logging.getLogger('IceHub')
VERIFY_BUNDLE_FLAG = '--verify-bundle'


def _configure_bootstrap_logging() -> None:
    """Capture failures that happen before the full backend logger is imported."""

    if LOG.handlers:
        return
    try:
        root = Path(os.environ.get('LOCALAPPDATA') or (Path.home() / 'AppData' / 'Local'))
        log_dir = root / 'IceHub' / 'logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            log_dir / 'icehub-bootstrap.log',
            maxBytes=512 * 1024,
            backupCount=1,
            encoding='utf-8',
        )
        handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        LOG.addHandler(handler)
        LOG.setLevel(logging.INFO)
    except OSError:
        pass


def _report_unhandled_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    traceback: TracebackType | None,
) -> None:
    """Record uncaught exceptions without allowing the hook itself to crash."""

    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, traceback)
        return

    try:
        LOG.critical(
            'Unhandled exception',
            exc_info=(exc_type, exc_value, traceback),
        )
    except BaseException:
        # Logging must never hide the original exception.
        sys.__excepthook__(exc_type, exc_value, traceback)


def _report_thread_exception(args: threading.ExceptHookArgs) -> None:
    """Forward otherwise-unhandled thread failures to the application log."""

    # Match Python's default threading hook: SystemExit is a normal thread exit.
    if issubclass(args.exc_type, SystemExit):
        return
    _report_unhandled_exception(args.exc_type, args.exc_value, args.exc_traceback)


def _normalise_exit_code(code: object) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    LOG.error('Application requested a non-numeric exit: %s', code)
    return 1


def _verify_bundled_krkrz_resource() -> int:
    """Verify the packaged KRKRZ helper without importing the Qt application."""

    try:
        from backend.installers.krkrz import bundled_krkrz_dll
        from backend.platform.files import _validate_pe_file

        resource = bundled_krkrz_dll()
        if resource is None:
            raise FileNotFoundError('bundled KRKRZ Version.dll was not found')
        _validate_pe_file(resource)
    except Exception as error:
        LOG.error('Bundle verification failed: %s', error, exc_info=True)
        print(f'Bundle verification failed: {error}', file=sys.stderr)
        return 1

    print(f'Bundle verification succeeded: {resource}')
    return 0


def main(*, argv: Sequence[str] | None = None) -> int:
    """Start the Qt application and convert all exits to an integer status."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    _configure_bootstrap_logging()
    previous_exception_hook = sys.excepthook
    previous_thread_hook = threading.excepthook
    sys.excepthook = _report_unhandled_exception
    threading.excepthook = _report_thread_exception

    try:
        if VERIFY_BUNDLE_FLAG in arguments:
            return _verify_bundled_krkrz_resource()

        # Import after installing the hooks so startup/import failures are reported too.
        from app.bootstrap import main as run_application

        try:
            result = run_application()
        except SystemExit as exit_request:
            return _normalise_exit_code(exit_request.code)
        return _normalise_exit_code(result)
    except KeyboardInterrupt:
        LOG.info('Application interrupted by the user')
        return 130
    except BaseException:
        exc_type, exc_value, traceback = sys.exc_info()
        if exc_type is not None and exc_value is not None:
            _report_unhandled_exception(exc_type, exc_value, traceback)
        return 1
    finally:
        sys.excepthook = previous_exception_hook
        threading.excepthook = previous_thread_hook


if __name__ == '__main__':
    raise SystemExit(main())
