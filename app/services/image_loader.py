"""Qt-native asynchronous image loading with Steam fallbacks."""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Callable
from typing import Optional

from PyQt6.QtCore import QObject, QTimer, QUrl
from PyQt6.QtGui import QPixmap
from PyQt6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

from backend.cache import set_cached_header
from backend.runtime import BackendRuntime, resolve_runtime


ImageCallback = Callable[[QPixmap], None]
PendingImageRequest = tuple[ImageCallback, Optional[str], Optional[str]]


class ImageLoader(QObject):
    """Process-wide Qt image loader with request coalescing and fallbacks."""

    _instance: Optional['ImageLoader'] = None
    _cache: OrderedDict[str, QPixmap] = OrderedDict()
    _CACHE_MAX_ENTRIES = 128
    _CACHE_MAX_BYTES = 64 * 1024 * 1024

    @classmethod
    def instance(
        cls,
        *,
        runtime: BackendRuntime | None = None,
    ) -> 'ImageLoader':
        if cls._instance is None:
            cls._instance = cls(runtime=runtime)
        elif runtime is not None and cls._instance.runtime is not runtime:
            raise RuntimeError(
                'ImageLoader singleton already belongs to a different runtime'
            )
        return cls._instance

    def __init__(
        self,
        *,
        runtime: BackendRuntime | None = None,
    ) -> None:
        super().__init__()
        self.runtime = resolve_runtime(runtime)
        self.nam = QNetworkAccessManager(self)
        self._pending: dict[str, list[PendingImageRequest]] = {}
        self._api_pending: dict[str, list[ImageCallback]] = {}
        self._replies: set[QNetworkReply] = set()
        self._shutting_down = False

    @classmethod
    def _ordered_cache(cls) -> OrderedDict[str, QPixmap]:
        """Normalize legacy direct assignments while retaining insertion order."""

        if not isinstance(cls._cache, OrderedDict):
            cls._cache = OrderedDict(cls._cache.items())
        return cls._cache

    @staticmethod
    def _pixmap_cost(pixmap: QPixmap) -> int:
        """Return a conservative in-memory byte estimate for one pixmap."""

        try:
            return max(int(pixmap.toImage().sizeInBytes()), 0)
        except (AttributeError, RuntimeError, TypeError):
            return max(pixmap.width() * pixmap.height() * 4, 0)

    @classmethod
    def _cache_get(cls, url: str) -> QPixmap | None:
        cache = cls._ordered_cache()
        try:
            pixmap = cache.pop(url)
        except KeyError:
            return None
        cache[url] = pixmap
        return pixmap

    @classmethod
    def _cache_put(cls, url: str, pixmap: QPixmap) -> None:
        cache = cls._ordered_cache()
        cache.pop(url, None)
        cache[url] = pixmap
        while cache and (
            len(cache) > cls._CACHE_MAX_ENTRIES
            or sum(cls._pixmap_cost(item) for item in cache.values())
            > cls._CACHE_MAX_BYTES
        ):
            cache.popitem(last=False)

    @staticmethod
    def _deliver(callback: ImageCallback, pixmap: QPixmap) -> None:
        """Deliver on the GUI event loop and tolerate deleted target rows."""

        def invoke() -> None:
            try:
                callback(pixmap)
            except (RuntimeError, TypeError):
                pass

        QTimer.singleShot(0, invoke)

    def load(
        self,
        url: str,
        callback: ImageCallback,
        *,
        appid: Optional[str] = None,
        fallback_url: Optional[str] = None,
    ) -> None:
        """Load ``url`` and deliver it, then try fallback/API resolution."""

        if not url:
            url = fallback_url or ''
        if self._shutting_down or not url:
            return
        cached = self._cache_get(url)
        if cached is not None:
            self._deliver(callback, cached)
            return
        if url in self._pending:
            self._pending[url].append((callback, appid, fallback_url))
            return

        self._pending[url] = [(callback, appid, fallback_url)]
        request = QNetworkRequest(QUrl(url))
        request.setRawHeader(b'User-Agent', b'Mozilla/5.0')
        reply = self.nam.get(request)
        self._replies.add(reply)
        timeout = QTimer(reply)
        timeout.setSingleShot(True)
        timeout.timeout.connect(reply.abort)
        timeout.start(15_000)

        def on_finished() -> None:
            requests = self._pending.pop(url, [])
            try:
                loaded = False
                if reply.error() == QNetworkReply.NetworkError.NoError:
                    pixmap = QPixmap()
                    pixmap.loadFromData(reply.readAll())
                    if not pixmap.isNull():
                        self._cache_put(url, pixmap)
                        for pending_callback, _appid, _fallback in requests:
                            self._deliver(pending_callback, pixmap)
                        loaded = True
                if not loaded:
                    for pending_callback, pending_appid, pending_fallback in (
                        requests if not self._shutting_down else []
                    ):
                        self._handle_failure(
                            url,
                            pending_callback,
                            appid=pending_appid,
                            fallback_url=pending_fallback,
                        )
            finally:
                self._replies.discard(reply)
                reply.deleteLater()

        reply.finished.connect(on_finished)

    def _handle_failure(
        self,
        failed_url: str,
        callback: ImageCallback,
        *,
        appid: Optional[str],
        fallback_url: Optional[str],
    ) -> None:
        no_hash_url = re.sub(
            r'/apps/(\d+)/[a-f0-9]{20,}/',
            r'/apps/\1/',
            failed_url,
        )
        if no_hash_url != failed_url:
            self.load(
                no_hash_url,
                callback,
                appid=appid,
                fallback_url=fallback_url,
            )
            return
        if fallback_url and fallback_url != failed_url:
            self.load(fallback_url, callback, appid=appid)
            return
        if appid:
            self._fetch_real_url_via_api(appid, callback)

    def _fetch_real_url_via_api(
        self,
        appid: str,
        callback: ImageCallback,
    ) -> None:
        """Resolve a Steam app's current image URL and retry it."""

        if appid in self._api_pending:
            self._api_pending[appid].append(callback)
            return
        self._api_pending[appid] = [callback]
        api_url = (
            'https://store.steampowered.com/api/appdetails'
            f'?appids={appid}&l=tchinese&filters=basic'
        )
        request = QNetworkRequest(QUrl(api_url))
        request.setRawHeader(b'User-Agent', b'Mozilla/5.0')
        request.setRawHeader(
            b'Accept-Language',
            b'zh-TW,zh;q=0.9,en;q=0.8',
        )
        cookie_header = (
            'birthtime=631152000; mature_content=1; wants_mature_content=1; '
            'lastagecheckage=1-January-1980'
        )
        request.setRawHeader(b'Cookie', cookie_header.encode())
        reply = self.nam.get(request)
        self._replies.add(reply)
        timeout = QTimer(reply)
        timeout.setSingleShot(True)
        timeout.timeout.connect(reply.abort)
        timeout.start(15_000)

        def on_finished() -> None:
            callbacks = self._api_pending.pop(appid, [])
            try:
                if reply.error() != QNetworkReply.NetworkError.NoError:
                    return
                try:
                    body = bytes(reply.readAll()).decode(
                        'utf-8',
                        errors='replace',
                    )
                    data = json.loads(body)
                except Exception:
                    return
                entry = data.get(str(appid), {})
                if not entry.get('success'):
                    return
                details = entry.get('data', {}) or {}
                real_url = (
                    details.get('header_image')
                    or details.get('capsule_image')
                    or ''
                )
                if not real_url:
                    return
                set_cached_header(appid, real_url, runtime=self.runtime)
                if not self._shutting_down:
                    for pending_callback in callbacks:
                        self.load(real_url, pending_callback)
            finally:
                self._replies.discard(reply)
                reply.deleteLater()

        reply.finished.connect(on_finished)

    def shutdown(self) -> None:
        """Abort requests without scheduling fallback work during app exit."""

        self._shutting_down = True
        self._pending.clear()
        self._api_pending.clear()
        self._cache.clear()
        for reply in tuple(self._replies):
            reply.abort()
        self._replies.clear()
        cls = type(self)
        if cls._instance is self:
            cls._instance = None


__all__ = ['ImageCallback', 'ImageLoader']
