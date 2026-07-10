from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Iterator
from pathlib import Path

import pytest
from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, QObject, pyqtSignal
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtNetwork import QNetworkReply, QNetworkRequest

import app.services.image_loader as image_loader_module
from app.services.image_loader import ImageLoader
from backend.runtime import BackendRuntime
from backend.storage import AppPaths


class FakeReply(QObject):
    finished = pyqtSignal()

    def __init__(
        self,
        *,
        error: QNetworkReply.NetworkError,
        data: bytes = b'',
    ) -> None:
        super().__init__()
        self._error = error
        self._data = data
        self.aborted = False

    def error(self) -> QNetworkReply.NetworkError:
        return self._error

    def readAll(self) -> QByteArray:
        return QByteArray(self._data)

    def abort(self) -> None:
        self.aborted = True


class FakeNetworkManager:
    def __init__(self, *replies: FakeReply) -> None:
        self.replies = list(replies)
        self.requests: list[QNetworkRequest] = []

    def get(self, request: QNetworkRequest) -> FakeReply:
        self.requests.append(request)
        return self.replies.pop(0)


@pytest.fixture
def loader(qapp) -> Iterator[ImageLoader]:
    del qapp
    previous_instance = ImageLoader._instance
    previous_cache = ImageLoader._cache
    ImageLoader._instance = None
    ImageLoader._cache = OrderedDict()
    current = ImageLoader.instance()
    yield current
    current.shutdown()
    ImageLoader._instance = previous_instance
    ImageLoader._cache = previous_cache


def _png_bytes() -> bytes:
    pixmap = QPixmap(2, 2)
    pixmap.fill(QColor('orange'))
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert pixmap.save(buffer, 'PNG')
    buffer.close()
    return bytes(data)


def test_loader_is_a_process_singleton(loader: ImageLoader) -> None:
    assert ImageLoader.instance() is loader
    assert ImageLoader.instance() is loader


def test_cached_pixmap_is_delivered_without_network(
    loader: ImageLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pixmap = QPixmap(1, 1)
    ImageLoader._cache['https://cdn.test/cached.jpg'] = pixmap
    delivered: list[QPixmap] = []
    monkeypatch.setattr(
        ImageLoader,
        '_deliver',
        staticmethod(lambda callback, image: callback(image)),
    )
    manager = FakeNetworkManager()
    loader.nam = manager  # type: ignore[assignment]

    loader.load('https://cdn.test/cached.jpg', delivered.append)

    assert delivered == [pixmap]
    assert manager.requests == []


def test_cache_hit_promotes_entry_before_lru_eviction(
    loader: ImageLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ImageLoader, '_CACHE_MAX_ENTRIES', 2)
    monkeypatch.setattr(ImageLoader, '_CACHE_MAX_BYTES', 1024 * 1024)
    monkeypatch.setattr(
        ImageLoader,
        '_deliver',
        staticmethod(lambda callback, image: callback(image)),
    )
    first = QPixmap(1, 1)
    second = QPixmap(1, 1)
    third = QPixmap(1, 1)
    ImageLoader._cache_put('first', first)
    ImageLoader._cache_put('second', second)

    delivered: list[QPixmap] = []
    loader.load('first', delivered.append)
    ImageLoader._cache_put('third', third)

    assert delivered == [first]
    assert list(ImageLoader._cache) == ['first', 'third']


def test_cache_enforces_memory_cap(
    loader: ImageLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del loader
    first = QPixmap(4, 4)
    second = QPixmap(4, 4)
    one_pixmap = ImageLoader._pixmap_cost(first)
    monkeypatch.setattr(ImageLoader, '_CACHE_MAX_ENTRIES', 10)
    monkeypatch.setattr(ImageLoader, '_CACHE_MAX_BYTES', one_pixmap + 1)

    ImageLoader._cache_put('first', first)
    ImageLoader._cache_put('second', second)

    assert list(ImageLoader._cache) == ['second']
    assert sum(
        ImageLoader._pixmap_cost(pixmap)
        for pixmap in ImageLoader._cache.values()
    ) <= ImageLoader._CACHE_MAX_BYTES


def test_duplicate_network_loads_share_reply_and_keep_callback_metadata(
    loader: ImageLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = FakeReply(error=QNetworkReply.NetworkError.ContentNotFoundError)
    manager = FakeNetworkManager(reply)
    loader.nam = manager  # type: ignore[assignment]
    failures: list[tuple[str, str | None, str | None]] = []
    monkeypatch.setattr(
        loader,
        '_handle_failure',
        lambda failed_url, _callback, *, appid, fallback_url: failures.append(
            (failed_url, appid, fallback_url)
        ),
    )

    url = 'https://cdn.test/missing.jpg'
    loader.load(url, lambda _image: None, appid='10', fallback_url='first.jpg')
    loader.load(url, lambda _image: None, appid='20', fallback_url='second.jpg')

    assert len(manager.requests) == 1
    reply.finished.emit()
    assert failures == [
        (url, '10', 'first.jpg'),
        (url, '20', 'second.jpg'),
    ]
    assert reply not in loader._replies


def test_successful_network_image_is_cached_and_delivered(
    loader: ImageLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reply = FakeReply(
        error=QNetworkReply.NetworkError.NoError,
        data=_png_bytes(),
    )
    loader.nam = FakeNetworkManager(reply)  # type: ignore[assignment]
    delivered: list[QPixmap] = []
    monkeypatch.setattr(
        ImageLoader,
        '_deliver',
        staticmethod(lambda callback, image: callback(image)),
    )

    url = 'https://cdn.test/image.png'
    loader.load(url, delivered.append)
    reply.finished.emit()

    assert len(delivered) == 1
    assert delivered[0].isNull() is False
    assert ImageLoader._cache[url].isNull() is False


def test_failure_chain_prefers_hashless_then_fallback_then_api(
    loader: ImageLoader,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads: list[tuple[str, str | None, str | None]] = []
    api_calls: list[str] = []
    monkeypatch.setattr(
        loader,
        'load',
        lambda url, _callback, *, appid=None, fallback_url=None: loads.append(
            (url, appid, fallback_url)
        ),
    )
    monkeypatch.setattr(
        loader,
        '_fetch_real_url_via_api',
        lambda appid, _callback: api_calls.append(appid),
    )
    def callback(_image: QPixmap) -> None:
        pass

    loader._handle_failure(
        'https://cdn.test/apps/480/abcdef0123456789abcd/header.jpg',
        callback,
        appid='480',
        fallback_url='https://cdn.test/fallback.jpg',
    )
    loader._handle_failure(
        'https://cdn.test/apps/480/header.jpg',
        callback,
        appid='480',
        fallback_url='https://cdn.test/fallback.jpg',
    )
    loader._handle_failure(
        'https://cdn.test/apps/480/header.jpg',
        callback,
        appid='480',
        fallback_url=None,
    )

    assert loads == [
        (
            'https://cdn.test/apps/480/header.jpg',
            '480',
            'https://cdn.test/fallback.jpg',
        ),
        ('https://cdn.test/fallback.jpg', '480', None),
    ]
    assert api_calls == ['480']


def test_api_resolution_coalesces_callbacks_and_retries_real_url(
    loader: ImageLoader,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    paths = AppPaths.discover(
        development_root=tmp_path / 'data',
        resource_root=tmp_path / 'resources',
    )
    paths.ensure_writable_dirs()
    runtime = BackendRuntime.from_paths(paths, legacy_root=tmp_path / 'legacy')
    loader.runtime = runtime
    real_url = 'https://cdn.test/480/header.jpg'
    reply = FakeReply(
        error=QNetworkReply.NetworkError.NoError,
        data=json.dumps(
            {
                '480': {
                    'success': True,
                    'data': {'header_image': real_url},
                }
            }
        ).encode(),
    )
    manager = FakeNetworkManager(reply)
    loader.nam = manager  # type: ignore[assignment]
    cached: list[tuple[str, str, BackendRuntime | None]] = []
    retried: list[tuple[str, object]] = []
    monkeypatch.setattr(
        image_loader_module,
        'set_cached_header',
        lambda appid, url, *, runtime=None: cached.append(
            (appid, url, runtime)
        ),
    )
    monkeypatch.setattr(
        loader,
        'load',
        lambda url, callback, **_kwargs: retried.append((url, callback)),
    )
    def first(_image: QPixmap) -> None:
        pass

    def second(_image: QPixmap) -> None:
        pass

    loader._fetch_real_url_via_api('480', first)
    loader._fetch_real_url_via_api('480', second)

    assert len(manager.requests) == 1
    request = manager.requests[0]
    assert 'appids=480' in request.url().toString()
    assert b'wants_mature_content=1' in bytes(request.rawHeader(b'Cookie'))
    reply.finished.emit()

    assert cached == [('480', real_url, runtime)]
    assert retried == [(real_url, first), (real_url, second)]
    assert '480' not in loader._api_pending


def test_shutdown_clears_state_and_aborts_active_replies(
    loader: ImageLoader,
) -> None:
    reply = FakeReply(error=QNetworkReply.NetworkError.OperationCanceledError)
    loader._pending['url'] = []
    loader._api_pending['480'] = []
    ImageLoader._cache['url'] = QPixmap(1, 1)
    loader._replies.add(reply)  # type: ignore[arg-type]

    loader.shutdown()

    assert loader._shutting_down is True
    assert loader._pending == {}
    assert loader._api_pending == {}
    assert ImageLoader._cache == {}
    assert reply.aborted is True
    assert loader._replies == set()
    assert ImageLoader._instance is None


def test_shutdown_allows_singleton_recreation_with_injected_runtime(
    loader: ImageLoader,
    tmp_path: Path,
) -> None:
    paths = AppPaths.discover(
        development_root=tmp_path / 'data',
        resource_root=tmp_path / 'resources',
    )
    runtime = BackendRuntime.from_paths(paths, legacy_root=tmp_path / 'legacy')

    loader.shutdown()
    replacement = ImageLoader.instance(runtime=runtime)
    try:
        assert replacement is not loader
        assert replacement.runtime is runtime
        assert replacement._shutting_down is False
    finally:
        replacement.shutdown()


def test_singleton_rejects_switching_runtime_while_live(
    loader: ImageLoader,
    tmp_path: Path,
) -> None:
    paths = AppPaths.discover(development_root=tmp_path / 'other-runtime')
    runtime = BackendRuntime.from_paths(paths, legacy_root=tmp_path / 'legacy')

    with pytest.raises(RuntimeError, match='different runtime'):
        ImageLoader.instance(runtime=runtime)
