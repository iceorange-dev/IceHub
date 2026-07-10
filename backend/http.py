"""Bounded and authenticated HTTP download primitives for IceHub."""

from __future__ import annotations

from collections.abc import Set
import hashlib
import logging
import os
from pathlib import Path
from typing import Callable, Optional
import urllib.parse
import uuid

import httpx

from .constants import GITHUB_DOWNLOAD_HOSTS, MAX_RELEASE_DOWNLOAD_BYTES
from .releases import github_digest_hex
from .security import SecurityValidationError


LOG = logging.getLogger('IceHub')


def validated_https_url(url: str, *, allowed_hosts: Set[str]) -> str:
    """Return an HTTPS URL only when its host is explicitly allowed."""

    try:
        parsed = urllib.parse.urlsplit(url)
        host = (parsed.hostname or '').lower()
    except ValueError as error:
        raise SecurityValidationError(f'URL 格式無效: {url!r}') from error
    if parsed.scheme.lower() != 'https' or not host or host not in allowed_hosts:
        raise SecurityValidationError(f'不允許的下載來源: {url!r}')
    if parsed.username or parsed.password:
        raise SecurityValidationError('URL 不得包含帳號或密碼')
    return url


_validated_https_url = validated_https_url


async def read_response_limited(response: httpx.Response, *, max_bytes: int) -> bytes:
    """Read a response body while enforcing declared and streamed limits."""

    declared = int(response.headers.get('Content-Length', '0') or 0)
    if declared > max_bytes:
        raise SecurityValidationError(f'遠端回應超過大小上限 ({declared} bytes)')
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise SecurityValidationError(f'遠端回應超過大小上限 ({max_bytes} bytes)')
        chunks.append(chunk)
    return b''.join(chunks)


_read_response_limited = read_response_limited


async def download_file(
    client: httpx.AsyncClient,
    url: str,
    dest: Path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    *,
    expected_size: int = 0,
    expected_digest: str = '',
    max_bytes: int = MAX_RELEASE_DOWNLOAD_BYTES,
    allowed_hosts: Optional[Set[str]] = None,
) -> bool:
    """Download to a temporary file, verify it, then atomically publish it."""

    part_path = dest.with_name(f'.{dest.name}.{uuid.uuid4().hex}.part')
    try:
        allowed = (
            GITHUB_DOWNLOAD_HOSTS
            if allowed_hosts is None
            else allowed_hosts
        )
        validated_https_url(url, allowed_hosts=allowed)
        digest_hex = github_digest_hex(expected_digest)
        if expected_size <= 0 or expected_size > max_bytes:
            raise SecurityValidationError('release asset size 缺失或超過安全上限')
        async with client.stream('GET', url, timeout=120, follow_redirects=True) as response:
            validated_https_url(str(response.url), allowed_hosts=allowed)
            if response.status_code != 200:
                LOG.error('下載失敗 HTTP %s: %s', response.status_code, url)
                return False
            total = int(response.headers.get('Content-Length', '0') or 0)
            if total and total != expected_size:
                raise SecurityValidationError(
                    f'下載 Content-Length ({total}) 與 GitHub asset size ({expected_size}) 不符'
                )
            if total > max_bytes:
                raise SecurityValidationError('下載檔案超過安全大小上限')

            dest.parent.mkdir(parents=True, exist_ok=True)
            current = 0
            hasher = hashlib.sha256()
            with part_path.open('xb') as stream:
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    current += len(chunk)
                    if current > max_bytes or current > expected_size:
                        raise SecurityValidationError('下載內容超過宣告大小')
                    stream.write(chunk)
                    hasher.update(chunk)
                    if progress_cb:
                        try:
                            progress_cb(current, total)
                        except Exception:
                            pass
                stream.flush()
                os.fsync(stream.fileno())

            if current != expected_size:
                raise SecurityValidationError(f'下載大小不符：預期 {expected_size}，實際 {current}')
            if hasher.hexdigest() != digest_hex:
                raise SecurityValidationError('下載檔案 SHA-256 驗證失敗')
            os.replace(part_path, dest)
            return True
    except Exception as error:
        LOG.error('下載 %s 時出錯: %s', url, error)
        return False
    finally:
        part_path.unlink(missing_ok=True)


__all__ = [
    'download_file',
    'read_response_limited',
    'validated_https_url',
]
