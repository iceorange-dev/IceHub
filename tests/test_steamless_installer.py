from __future__ import annotations

from pathlib import Path
import zipfile

import backend.ost_backend as facade
from backend.installers import steamless
from backend.releases import pick_steamless_zip as release_picker
from backend.runtime import BackendRuntime
from backend.storage import AppPaths


def _pe(payload: bytes) -> bytes:
    return b'MZ' + (b'\0' * 62) + payload


def _steamless_zip(path: Path) -> Path:
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('package/Steamless.CLI.exe', _pe(b'cli'))
        archive.writestr('package/Plugins/Steamless.Plugin.dll', _pe(b'plugin'))
    return path


def test_legacy_facade_statically_reexports_steamless_domain() -> None:
    exports = (
        'install_steamless_from_zip',
        'is_steamless_installed',
        'pick_steamless_zip',
        'uninstall_steamless',
        '_read_steamless_internal_manifest',
        '_recover_steamless_removal',
        '_steamless_lock_path',
        '_steamless_remove_journal_path',
        '_validate_steamless_ownership',
    )
    for name in exports:
        assert getattr(facade, name) is getattr(steamless, name)

    assert facade.STEAMLESS_DIR == steamless.STEAMLESS_DIR
    assert facade.STEAMLESS_CLI == steamless.STEAMLESS_CLI
    assert facade.STEAMLESS_MANIFEST == steamless.STEAMLESS_MANIFEST
    assert facade._steamless_manifest_store is steamless._steamless_manifest_store
    assert steamless.pick_steamless_zip is release_picker


def test_direct_domain_install_status_and_uninstall(tmp_path: Path) -> None:
    paths = AppPaths.discover(development_root=tmp_path / 'data')
    paths.ensure_writable_dirs()
    runtime = BackendRuntime.from_paths(paths)
    archive = _steamless_zip(tmp_path / 'steamless.zip')

    installed, message, deployed = steamless.install_steamless_from_zip(
        archive,
        version='v-test',
        runtime=runtime,
    )

    assert installed is True, message
    assert steamless.is_steamless_installed(runtime=runtime) is True
    assert runtime.steamless_cli.is_file()
    assert any(Path(path).name == 'Steamless.CLI.exe' for path in deployed)

    removed, message = steamless.uninstall_steamless(runtime=runtime)

    assert removed is True, message
    assert not runtime.steamless_dir.exists()
    assert runtime.steamless_manifest_store.read() == {}


def test_direct_domain_refuses_unowned_tree(tmp_path: Path) -> None:
    paths = AppPaths.discover(development_root=tmp_path / 'data')
    paths.ensure_writable_dirs()
    runtime = BackendRuntime.from_paths(paths)
    runtime.steamless_dir.mkdir(parents=True)
    unmanaged = runtime.steamless_dir / 'user-tool.txt'
    unmanaged.write_bytes(b'user data')

    removed, _message = steamless.uninstall_steamless(runtime=runtime)

    assert removed is False
    assert unmanaged.read_bytes() == b'user data'
