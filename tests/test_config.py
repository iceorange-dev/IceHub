from __future__ import annotations

import json
from pathlib import Path

import backend.config as config
import backend.ost_backend as facade
from backend.constants import DEFAULT_CONFIG
from backend.runtime import BackendRuntime
from backend.storage import AppPaths, CredentialStore


class FakeCredentials(CredentialStore):
    backend = 'fake'
    secure = True

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete(self, name: str) -> None:
        self.values.pop(name, None)


def _runtime(tmp_path: Path) -> tuple[BackendRuntime, FakeCredentials]:
    paths = AppPaths.discover(
        development_root=tmp_path / 'data',
        resource_root=tmp_path / 'resources',
    )
    credentials = FakeCredentials()

    def factory(
        path: Path,
        *,
        allow_plaintext_fallback: bool = False,
    ) -> CredentialStore:
        del path, allow_plaintext_fallback
        return credentials

    runtime = BackendRuntime.from_paths(
        paths,
        legacy_root=tmp_path / 'legacy',
        credential_factory=factory,
    )
    return runtime, credentials


def test_load_config_initializes_metadata_caches_before_returning(
    tmp_path: Path,
) -> None:
    runtime, _credentials = _runtime(tmp_path)
    runtime.name_store.write({'480': 'Spacewar'})

    loaded = config.load_config(runtime=runtime)

    assert loaded['config_version'] == DEFAULT_CONFIG['config_version']
    assert runtime.name_cache == {'480': 'Spacewar'}
    assert runtime.config_file.is_file()


def test_secret_is_stored_in_credentials_and_never_plaintext_config(
    tmp_path: Path,
) -> None:
    runtime, credentials = _runtime(tmp_path)

    config.save_config(
        {
            **DEFAULT_CONFIG,
            'default_source': 'sudama',
            'Ryuu_Auth_Key': 'test-secret',
        },
        runtime=runtime,
    )

    assert credentials.get_ryuu_auth_key() == 'test-secret'
    assert 'Ryuu_Auth_Key' not in runtime.config_store.read()
    assert config.load_config(runtime=runtime)['Ryuu_Auth_Key'] == 'test-secret'


def test_legacy_config_migration_secures_key_and_normalizes_sources(
    tmp_path: Path,
) -> None:
    runtime, credentials = _runtime(tmp_path)
    legacy = runtime.legacy_root / 'config' / 'config.json'
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {
                'Ryuu_Auth_Key': 'legacy-secret',
                'Ryuu_API_Base': 'https://attacker.test',
                'theme_mode': 'dark',
            }
        ),
        encoding='utf-8',
    )

    loaded = config.load_config(runtime=runtime)

    assert loaded['theme_mode'] == 'dark'
    assert loaded['Ryuu_API_Base'] == DEFAULT_CONFIG['Ryuu_API_Base']
    assert loaded['Ryuu_Auth_Key'] == 'legacy-secret'
    assert credentials.get_ryuu_auth_key() == 'legacy-secret'
    assert 'Ryuu_Auth_Key' not in runtime.config_store.read()


def test_update_fields_preserves_unrelated_latest_values(tmp_path: Path) -> None:
    runtime, _credentials = _runtime(tmp_path)
    config.save_config(
        {**DEFAULT_CONFIG, 'theme_mode': 'dark', 'default_source': 'ryuu'},
        runtime=runtime,
    )

    updated = config.update_config_fields(
        {'default_source': 'sudama'},
        runtime=runtime,
    )

    assert updated['default_source'] == 'sudama'
    assert updated['theme_mode'] == 'dark'


def test_legacy_facade_reexports_config_api() -> None:
    assert facade.load_config is config.load_config
    assert facade.save_config is config.save_config
    assert facade.update_config_fields is config.update_config_fields
