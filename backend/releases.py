"""GitHub release metadata and version-selection helpers.

This module is deliberately independent of the application runtime and domain
installers.  Callers supply an HTTP client and an optional repository
allowlist; the returned dictionaries retain the legacy facade shape.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, Optional, Tuple

import httpx
from packaging.version import InvalidVersion, Version

from .constants import (
    GITHUB_API,
    OST_DEFAULT_REPO,
    TRUSTED_LEGACY_ASSET_DIGESTS,
)
from .security import SecurityValidationError, validate_github_repo


LOG = logging.getLogger('IceHub')


def github_digest_hex(digest: str) -> str:
    """Return the hexadecimal part of a GitHub ``sha256:...`` digest."""

    if not isinstance(digest, str) or not digest.startswith('sha256:'):
        raise SecurityValidationError('GitHub release asset 缺少 SHA-256 digest')
    value = digest.removeprefix('sha256:').lower()
    if re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise SecurityValidationError('GitHub release asset digest 格式無效')
    return value


# Preserve the old helper name for compatibility with callers that imported
# the implementation detail before ``ost_backend`` became a facade.
_github_digest_hex = github_digest_hex


def _version_tuple(v: str) -> Tuple[int, ...]:
    """Extract a numeric fallback tuple from a non-PEP-440 version string."""

    numbers = re.findall(r'\d+', v or '')
    if not numbers:
        return (0,)
    return tuple(int(number) for number in numbers)


def version_newer(a: str, b: str) -> bool:
    """Return whether version ``a`` is newer than version ``b``."""

    try:
        return Version((a or '').lstrip('vV')) > Version((b or '').lstrip('vV'))
    except InvalidVersion:
        return _version_tuple(a) > _version_tuple(b)


async def fetch_latest_release(
    client: httpx.AsyncClient,
    repo: str = OST_DEFAULT_REPO,
    *,
    allowed_repos: Optional[set[str]] = None,
) -> Optional[Dict]:
    """Fetch and normalize the latest GitHub release for an allowed repo."""

    try:
        repo = validate_github_repo(repo, allowlist=allowed_repos)
        response = await client.get(
            f'{GITHUB_API}/repos/{repo}/releases/latest',
            headers={'Accept': 'application/vnd.github+json'},
            timeout=20,
        )
        if response.status_code != 200:
            LOG.warning('GitHub API 回傳 HTTP %s', response.status_code)
            return None

        data = response.json()
        tag_name = str(data.get('tag_name', ''))
        assets = []
        for raw_asset in data.get('assets', []) or []:
            name = str(raw_asset.get('name', ''))
            size = int(raw_asset.get('size', 0) or 0)
            digest = raw_asset.get('digest') or TRUSTED_LEGACY_ASSET_DIGESTS.get(
                (repo.casefold(), tag_name, name, size),
                '',
            )
            assets.append(
                {
                    'name': name,
                    'browser_download_url': raw_asset.get('browser_download_url', ''),
                    'size': size,
                    'digest': digest,
                    'content_type': raw_asset.get('content_type', ''),
                    'state': raw_asset.get('state', ''),
                }
            )
        return {
            'tag_name': tag_name,
            'name': data.get('name', ''),
            'published_at': data.get('published_at', ''),
            'html_url': data.get('html_url', ''),
            'body': data.get('body', '')[:2000],
            'assets': assets,
        }
    except Exception as error:
        LOG.error('抓取 GitHub release 失敗: %s', error)
        return None


def _release_zip_candidates(release: Dict) -> list[Dict]:
    if not release:
        return []
    return [
        asset
        for asset in release.get('assets', []) or []
        if str(asset.get('name', '')).lower().endswith('.zip')
        and asset.get('state') in {'uploaded', ''}
        and asset.get('digest')
    ]


def pick_release_zip(release: Dict) -> Optional[Dict]:
    """Select the main OpenSteamTool zip from normalized release assets."""

    candidates = _release_zip_candidates(release)
    preferred = [asset for asset in candidates if 'release' in asset['name'].lower()]
    return (preferred or candidates or [None])[0]


def pick_steamless_zip(release: Dict) -> Optional[Dict]:
    """Select the official atom0s Steamless zip from release assets."""

    candidates = _release_zip_candidates(release)
    preferred = [asset for asset in candidates if 'atom0s' in asset['name'].lower()]
    return (preferred or candidates or [None])[0]


__all__ = [
    'fetch_latest_release',
    'github_digest_hex',
    'pick_release_zip',
    'pick_steamless_zip',
    'version_newer',
]
