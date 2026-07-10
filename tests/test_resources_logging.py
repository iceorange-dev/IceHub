"""Tests for extracted resource discovery and logger setup leaves."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import uuid

import backend.ost_backend as backend_facade
from backend.logging_setup import setup_logger
from backend.resources import resource_path
from backend.runtime import BackendRuntime
from backend.storage import AppPaths


def _runtime(tmp_path: Path) -> BackendRuntime:
    paths = AppPaths.discover(
        development_root=tmp_path / 'data',
        resource_root=tmp_path / 'resources',
    )
    paths.ensure_writable_dirs()
    logger = logging.getLogger(f'IceHub.test.{uuid.uuid4().hex}')
    return BackendRuntime.from_paths(
        paths,
        legacy_root=tmp_path / 'legacy',
        log=logger,
    )


def test_resource_path_uses_injected_read_only_resource_root(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    expected = runtime.resource_root / 'isolated-only' / 'resource.bin'
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b'resource')

    assert resource_path('isolated-only/resource.bin', runtime=runtime) == expected


def test_setup_logger_uses_runtime_log_directory(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    logger = setup_logger(debug=True, runtime=runtime)
    try:
        assert logger is runtime.log
        assert logger.level == logging.DEBUG
        assert logger.propagate is False
        file_handlers = [
            handler
            for handler in logger.handlers
            if isinstance(handler, RotatingFileHandler)
        ]
        assert len(file_handlers) == 1
        assert Path(file_handlers[0].baseFilename) == (
            runtime.paths.log_dir / 'icehub.log'
        )
    finally:
        for handler in tuple(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_backend_facade_statically_reexports_leaf_helpers() -> None:
    assert backend_facade.resource_path is resource_path
    assert backend_facade.setup_logger is setup_logger
