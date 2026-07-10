from __future__ import annotations

import app.pages.home as home
import app.pages.installed as installed
import app.pages.ost as ost
from PyQt6.QtWidgets import QWidget

from app.pages.common import error_text, resolve_backend_provider
from app.services.context import AppServices
from app.tasking import WorkerFailure


def test_error_text_formats_worker_failures_and_plain_errors() -> None:
    assert error_text(WorkerFailure(RuntimeError('boom'), 'traceback')) == 'boom'
    assert error_text(WorkerFailure(RuntimeError(), 'traceback')) == 'RuntimeError'
    assert error_text(ValueError('plain')) == 'plain'


def test_completed_pages_statically_import_shared_error_text() -> None:
    assert home._error_text is error_text
    assert installed._error_text is error_text
    assert ost._error_text is error_text


def test_backend_provider_is_captured_from_services_with_standalone_fallback(
    qtbot,
) -> None:
    class InjectedBackend:
        pass

    injected = InjectedBackend()
    fallback = object()
    window = QWidget()
    page = QWidget(window)
    qtbot.addWidget(window)
    window.app_services = AppServices(backend_provider=lambda: injected)

    injected_provider = resolve_backend_provider(page, lambda: fallback)
    assert injected_provider() is injected

    standalone = QWidget()
    qtbot.addWidget(standalone)
    fallback_provider = resolve_backend_provider(standalone, lambda: fallback)
    assert fallback_provider() is fallback
