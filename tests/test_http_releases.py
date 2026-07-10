from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from backend.http import download_file, read_response_limited, validated_https_url
from backend.releases import (
    fetch_latest_release,
    github_digest_hex,
    pick_release_zip,
    pick_steamless_zip,
    version_newer,
)
from backend.security import SecurityValidationError


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.parametrize(
    'url',
    [
        'http://downloads.test/release.zip',
        'https://attacker.test/release.zip',
        'https://user:secret@downloads.test/release.zip',
    ],
)
def test_validated_https_url_rejects_untrusted_sources(url: str) -> None:
    with pytest.raises(SecurityValidationError):
        validated_https_url(url, allowed_hosts={'downloads.test'})

    assert (
        validated_https_url(
            'https://downloads.test/release.zip',
            allowed_hosts={'downloads.test'},
        )
        == 'https://downloads.test/release.zip'
    )


def test_response_reader_enforces_streamed_size_without_content_length() -> None:
    response = httpx.Response(
        200,
        request=httpx.Request('GET', 'https://downloads.test/data'),
        stream=ChunkStream(b'ab', b'cd'),
    )

    with pytest.raises(SecurityValidationError):
        asyncio.run(read_response_limited(response, max_bytes=3))


def test_download_empty_allowlist_fails_closed_instead_of_using_defaults(
    tmp_path,
) -> None:
    payload = b'verified payload'
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=payload,
            headers={'Content-Length': str(len(payload))},
        )

    async def exercise() -> tuple[bool, bool]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler)
        ) as client:
            common = {
                'expected_size': len(payload),
                'expected_digest': (
                    f'sha256:{hashlib.sha256(payload).hexdigest()}'
                ),
                'max_bytes': len(payload),
            }
            default_result = await download_file(
                client,
                'https://github.com/release.zip',
                tmp_path / 'default.zip',
                **common,
            )
            empty_result = await download_file(
                client,
                'https://github.com/release.zip',
                tmp_path / 'empty.zip',
                allowed_hosts=set(),
                **common,
            )
            return default_result, empty_result

    assert asyncio.run(exercise()) == (True, False)
    assert len(requests) == 1
    assert (tmp_path / 'default.zip').read_bytes() == payload
    assert not (tmp_path / 'empty.zip').exists()


def test_github_digest_and_version_comparison_are_strict() -> None:
    digest = 'a' * 64
    assert github_digest_hex(f'sha256:{digest.upper()}') == digest
    with pytest.raises(SecurityValidationError):
        github_digest_hex(digest)
    with pytest.raises(SecurityValidationError):
        github_digest_hex('sha256:not-hex')

    assert version_newer('v1.10.0', 'v1.9.9') is True
    assert version_newer('release-10-build', 'release-2-build') is True
    assert version_newer('1.0.0', '1.0.0') is False


def test_fetch_latest_release_normalizes_assets_and_uses_reviewed_legacy_digest() -> None:
    body = 'x' * 2100

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == 'https://api.github.com/repos/atom0s/Steamless/releases/latest'
        assert request.headers['Accept'] == 'application/vnd.github+json'
        return httpx.Response(
            200,
            json={
                'tag_name': 'v3.1.0.5',
                'name': 'Steamless',
                'published_at': '2023-01-01T00:00:00Z',
                'html_url': 'https://github.com/atom0s/Steamless/releases/tag/v3.1.0.5',
                'body': body,
                'assets': [
                    {
                        'name': 'Steamless.v3.1.0.5.-.by.atom0s.zip',
                        'browser_download_url': 'https://github.com/download.zip',
                        'size': 610646,
                        'content_type': 'application/zip',
                        'state': 'uploaded',
                    }
                ],
            },
        )

    async def exercise() -> dict | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_latest_release(
                client,
                'atom0s/Steamless',
                allowed_repos={'atom0s/Steamless'},
            )

    release = asyncio.run(exercise())

    assert release is not None
    assert len(release['body']) == 2000
    assert release['assets'][0]['digest'].startswith('sha256:')


def test_fetch_release_rejects_repo_before_network_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    async def exercise() -> dict | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_latest_release(
                client,
                'attacker/repo',
                allowed_repos={'trusted/repo'},
            )

    assert asyncio.run(exercise()) is None
    assert requests == []


def test_release_pickers_require_verified_uploaded_zip() -> None:
    ignored = {'name': 'unverified.zip', 'state': 'uploaded', 'digest': ''}
    fallback = {'name': 'fallback.zip', 'state': '', 'digest': 'sha256:' + '1' * 64}
    ost = {'name': 'OpenSteamTool.Release.zip', 'state': 'uploaded', 'digest': 'sha256:' + '2' * 64}
    steamless = {
        'name': 'Steamless.v1.by.atom0s.zip',
        'state': 'uploaded',
        'digest': 'sha256:' + '3' * 64,
    }

    assert pick_release_zip({'assets': [ignored, fallback, ost]}) is ost
    assert pick_steamless_zip({'assets': [ignored, fallback, steamless]}) is steamless
    assert pick_release_zip({'assets': [ignored]}) is None
    assert pick_steamless_zip({}) is None
