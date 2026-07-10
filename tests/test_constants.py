"""Contract tests for the static backend constants boundary."""

from __future__ import annotations

from types import MappingProxyType

import pytest

import backend.constants as constants
import backend.ost_backend as backend


REEXPORTED_CONSTANTS = (
    'DEFAULT_CONFIG',
    'GITHUB_API',
    'GITHUB_DOWNLOAD_HOSTS',
    'KRKRZ_BUNDLED_REL',
    'KRKRZ_DLL_FILENAME',
    'KRKRZ_REPO',
    'MAX_LUA_BYTES',
    'MAX_RELEASE_DOWNLOAD_BYTES',
    'MAX_SUDAMA_BYTES',
    'OST_DEFAULT_REPO',
    'OST_FILES',
    'STEAMLESS_DEFAULT_REPO',
    'STEAMLESS_FLAGS',
    'STEAMLESS_INTERNAL_MANIFEST',
    'TRUSTED_LEGACY_ASSET_DIGESTS',
    'TRUSTED_OST_FILE_HASHES',
)


def test_legacy_backend_facade_reexports_static_constants() -> None:
    for name in REEXPORTED_CONSTANTS:
        assert getattr(backend, name) is getattr(constants, name)


def test_default_config_references_named_trusted_endpoints_and_repositories() -> None:
    assert constants.DEFAULT_CONFIG['Ryuu_API_Base'] == constants.RYUU_API_BASE
    assert constants.DEFAULT_CONFIG['Sudama_API'] == constants.SUDAMA_API
    assert constants.DEFAULT_CONFIG['OST_Repo'] == constants.OST_DEFAULT_REPO
    assert (
        constants.DEFAULT_CONFIG['Steamless_Repo']
        == constants.STEAMLESS_DEFAULT_REPO
    )


def test_public_constant_containers_are_deeply_immutable() -> None:
    assert isinstance(constants.OST_FILES, tuple)
    assert isinstance(constants.STEAMLESS_FLAGS, tuple)
    assert isinstance(constants.GITHUB_DOWNLOAD_HOSTS, frozenset)
    assert isinstance(constants.DEFAULT_CONFIG, MappingProxyType)
    assert isinstance(constants.TRUSTED_LEGACY_ASSET_DIGESTS, MappingProxyType)
    assert isinstance(constants.TRUSTED_OST_FILE_HASHES, MappingProxyType)
    assert all(
        isinstance(digests, frozenset)
        for digests in constants.TRUSTED_OST_FILE_HASHES.values()
    )

    with pytest.raises(TypeError):
        constants.DEFAULT_CONFIG['debug_mode'] = True
    with pytest.raises(TypeError):
        constants.TRUSTED_OST_FILE_HASHES['new.dll'] = frozenset()
    with pytest.raises(AttributeError):
        constants.OST_FILES.append('untrusted.dll')
