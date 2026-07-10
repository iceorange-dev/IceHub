"""Application logger configuration independent from backend domains."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from .runtime import BackendRuntime, resolve_runtime


def setup_logger(
    debug: bool = False,
    *,
    runtime: BackendRuntime | None = None,
) -> logging.Logger:
    """Configure the runtime logger for console and rotating-file output."""

    active_runtime = resolve_runtime(runtime)
    logger = active_runtime.log
    for existing in tuple(logger.handlers):
        logger.removeHandler(existing)
        try:
            existing.close()
        except Exception:
            pass

    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%H:%M:%S',
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    try:
        active_runtime.paths.log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            active_runtime.paths.log_dir / 'icehub.log',
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding='utf-8',
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as error:
        logger.warning(f'無法建立檔案日誌: {error}')
    return logger


__all__ = ['setup_logger']
