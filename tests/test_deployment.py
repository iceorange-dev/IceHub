from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import backend.deployment as deployment_module
from backend.deployment import (
    DeploymentError,
    DirectorySwap,
    ManagedFileDeployment,
    OwnershipError,
    RollbackError,
)
from backend.security import sha256_file


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_install_update_preserves_first_original_and_uninstall_restores_it(tmp_path: Path):
    target = tmp_path / 'target'
    state = tmp_path / 'state'
    target.mkdir()
    _write(target / 'loader.dll', b'original')
    v1 = _write(tmp_path / 'stage1' / 'loader.dll', b'version one')
    v2 = _write(tmp_path / 'stage2' / 'loader.dll', b'version two')
    manager = ManagedFileDeployment(state, 'ost', target)

    first = manager.install({'loader.dll': v1}, version='1')
    assert (target / 'loader.dll').read_bytes() == b'version one'
    manifest = json.loads(first.manifest_path.read_text(encoding='utf-8'))
    backup = Path(manifest['files']['loader.dll']['original_backup'])
    assert backup.read_bytes() == b'original'

    manager.install({'loader.dll': v2}, version='2')
    assert backup.read_bytes() == b'original'
    assert (target / 'loader.dll').read_bytes() == b'version two'

    manager.uninstall()
    assert (target / 'loader.dll').read_bytes() == b'original'
    assert not manager.manifest_path.exists()


def test_install_and_uninstall_file_that_did_not_preexist(tmp_path: Path):
    target = tmp_path / 'target'
    target.mkdir()
    source = _write(tmp_path / 'source' / 'new.dll', b'new')
    manager = ManagedFileDeployment(tmp_path / 'state', 'ost', target)

    manager.install({'new.dll': source})
    assert (target / 'new.dll').exists()
    manager.uninstall()
    assert not (target / 'new.dll').exists()


def test_external_modification_blocks_update_and_uninstall(tmp_path: Path):
    target = tmp_path / 'target'
    target.mkdir()
    source = _write(tmp_path / 'source' / 'a.dll', b'managed')
    manager = ManagedFileDeployment(tmp_path / 'state', 'ost', target)
    manager.install({'a.dll': source})
    (target / 'a.dll').write_bytes(b'external change')

    with pytest.raises(OwnershipError):
        manager.install({'a.dll': source})
    with pytest.raises(OwnershipError):
        manager.uninstall()
    assert (target / 'a.dll').read_bytes() == b'external change'


def test_legacy_backup_override_becomes_original(tmp_path: Path):
    target = tmp_path / 'target'
    target.mkdir()
    _write(target / 'a.dll', b'legacy deployed')
    legacy_original = _write(tmp_path / 'legacy' / 'a.dll', b'real original')
    source = _write(tmp_path / 'source' / 'a.dll', b'new managed')
    manager = ManagedFileDeployment(tmp_path / 'state', 'ost', target)

    manager.install({'a.dll': source}, original_overrides={'a.dll': legacy_original})
    manager.uninstall()
    assert (target / 'a.dll').read_bytes() == b'real original'


def test_legacy_created_target_is_removed_on_uninstall(tmp_path: Path):
    target = tmp_path / 'target'
    target.mkdir()
    legacy = _write(target / 'a.dll', b'legacy deployed')
    manager = ManagedFileDeployment(tmp_path / 'state', 'ost', target)

    manager.install({'a.dll': legacy}, created_targets={'a.dll'})
    assert manager.status()['healthy'] is True
    manager.uninstall()
    assert not (target / 'a.dll').exists()


def test_manifest_records_deployed_hash(tmp_path: Path):
    target = tmp_path / 'target'
    target.mkdir()
    source = _write(tmp_path / 'source' / 'a.dll', b'payload')
    manager = ManagedFileDeployment(tmp_path / 'state', 'ost', target)
    result = manager.install({'a.dll': source}, version='v1')

    manifest = json.loads(result.manifest_path.read_text(encoding='utf-8'))
    assert manifest['version'] == 'v1'
    assert manifest['files']['a.dll']['deployed_sha256'] == sha256_file(target / 'a.dll')


def test_directory_swap_commit_replaces_tree(tmp_path: Path):
    target = tmp_path / 'tool'
    staging = tmp_path / '.tool-stage'
    _write(target / 'old.txt', b'old')
    _write(staging / 'new.txt', b'new')

    with DirectorySwap(staging, target) as swap:
        assert (target / 'new.txt').read_bytes() == b'new'
        swap.commit()

    assert not (target / 'old.txt').exists()
    assert not staging.exists()


def test_directory_swap_without_commit_rolls_back(tmp_path: Path):
    target = tmp_path / 'tool'
    staging = tmp_path / '.tool-stage'
    _write(target / 'old.txt', b'old')
    _write(staging / 'new.txt', b'new')

    with DirectorySwap(staging, target):
        assert (target / 'new.txt').exists()

    assert (target / 'old.txt').read_bytes() == b'old'
    assert not (target / 'new.txt').exists()


def test_tampered_deployment_id_cannot_escape_backup_root(tmp_path: Path):
    target = tmp_path / 'target'
    target.mkdir()
    _write(target / 'a.dll', b'original')
    source = _write(tmp_path / 'source' / 'a.dll', b'managed')
    outside = _write(tmp_path / 'must-not-delete' / 'sentinel.txt', b'keep')
    manager = ManagedFileDeployment(tmp_path / 'state', 'ost', target)
    manager.install({'a.dll': source})

    manifest = json.loads(manager.manifest_path.read_text(encoding='utf-8'))
    manifest['deployment_id'] = '../../must-not-delete'
    manager.manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

    with pytest.raises(DeploymentError):
        manager.uninstall()

    assert outside.read_bytes() == b'keep'
    assert (target / 'a.dll').read_bytes() == b'managed'


def test_tampered_original_backup_cannot_read_or_delete_outside_state_root(
    tmp_path: Path,
) -> None:
    target = tmp_path / 'target'
    target.mkdir()
    _write(target / 'a.dll', b'original')
    source = _write(tmp_path / 'source' / 'a.dll', b'managed')
    outside = _write(tmp_path / 'outside' / 'a.dll', b'attacker-selected backup')
    manager = ManagedFileDeployment(tmp_path / 'state', 'ost', target)
    manager.install({'a.dll': source})

    manifest = json.loads(manager.manifest_path.read_text(encoding='utf-8'))
    manifest['files']['a.dll']['original_backup'] = str(outside)
    manifest['files']['a.dll']['original_sha256'] = sha256_file(outside)
    manager.manifest_path.write_text(json.dumps(manifest), encoding='utf-8')

    with pytest.raises(DeploymentError):
        manager.uninstall()

    assert outside.read_bytes() == b'attacker-selected backup'
    assert (target / 'a.dll').read_bytes() == b'managed'


def test_case_insensitive_target_name_collision_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / 'target'
    target.mkdir()
    upper = _write(tmp_path / 'source-upper' / 'Loader.dll', b'upper')
    lower = _write(tmp_path / 'source-lower' / 'loader.dll', b'lower')
    manager = ManagedFileDeployment(tmp_path / 'state', 'ost', target)

    with pytest.raises(DeploymentError, match='case-insensitive'):
        manager.install({'Loader.dll': upper, 'loader.dll': lower})

    assert list(target.iterdir()) == []
    assert not manager.manifest_path.exists()


def test_symbolic_link_target_is_rejected_without_touching_link_destination(
    tmp_path: Path,
) -> None:
    target = tmp_path / 'target'
    target.mkdir()
    outside = _write(tmp_path / 'outside' / 'a.dll', b'outside')
    link = target / 'a.dll'
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f'symbolic links are unavailable: {error}')
    source = _write(tmp_path / 'source' / 'a.dll', b'managed')
    manager = ManagedFileDeployment(tmp_path / 'state', 'ost', target)

    with pytest.raises(OwnershipError):
        manager.install({'a.dll': source})

    assert link.is_symlink()
    assert outside.read_bytes() == b'outside'


def test_directory_swap_rejects_symlink_or_reparse_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path / 'outside'
    _write(outside / 'sentinel.txt', b'outside')
    target = tmp_path / 'tool'
    try:
        target.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f'directory links are unavailable: {error}')
    staging = tmp_path / '.tool-stage'
    _write(staging / 'new.txt', b'new')

    with pytest.raises(DeploymentError):
        with DirectorySwap(staging, target):
            pytest.fail('a link target must never be swapped')

    assert target.is_symlink()
    assert (outside / 'sentinel.txt').read_bytes() == b'outside'
    assert (staging / 'new.txt').read_bytes() == b'new'


def test_interrupted_managed_update_is_recovered_before_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process death after file replacement must not strand mixed state."""

    class SimulatedProcessDeath(BaseException):
        pass

    target = tmp_path / 'target'
    target.mkdir()
    v1 = _write(tmp_path / 'stage-v1' / 'a.dll', b'version one')
    v2 = _write(tmp_path / 'stage-v2' / 'a.dll', b'version two')
    state = tmp_path / 'state'
    manager = ManagedFileDeployment(state, 'ost', target)
    manager.install({'a.dll': v1}, version='1')

    real_atomic_write = deployment_module._atomic_json_write

    def die_before_manifest_publish(path: Path, data) -> None:
        if Path(path) == manager.manifest_path:
            raise SimulatedProcessDeath
        real_atomic_write(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(deployment_module, '_atomic_json_write', die_before_manifest_publish)
        with pytest.raises(SimulatedProcessDeath):
            manager.install({'a.dll': v2}, version='2')

    assert (target / 'a.dll').read_bytes() == b'version two'
    assert list((state / 'journals').glob('ost-*.json'))

    status = ManagedFileDeployment(state, 'ost', target).status()

    assert status['managed'] is True
    assert status['healthy'] is True
    assert (target / 'a.dll').read_bytes() == b'version one'
    assert not list((state / 'journals').glob('ost-*.json'))
    assert not list(target.glob('.icehub-transaction-*'))


def test_directory_swap_recovers_an_orphaned_uncommitted_swap(
    tmp_path: Path,
) -> None:
    target = tmp_path / 'tool'
    first_staging = tmp_path / '.tool-stage-one'
    second_staging = tmp_path / '.tool-stage-two'
    _write(target / 'old.txt', b'original')
    _write(first_staging / 'new.txt', b'orphaned replacement')

    orphaned = DirectorySwap(first_staging, target)
    orphaned.__enter__()  # Simulate process death: __exit__ never executes.
    assert (target / 'new.txt').read_bytes() == b'orphaned replacement'
    assert (tmp_path / '.tool.icehub-swap.json').is_file()

    _write(second_staging / 'next.txt', b'next replacement')
    with DirectorySwap(second_staging, target):
        assert (target / 'next.txt').read_bytes() == b'next replacement'

    assert (target / 'old.txt').read_bytes() == b'original'
    assert not (target / 'new.txt').exists()
    assert not (tmp_path / '.tool.icehub-swap.json').exists()


def test_prepared_directory_swap_preserves_target_created_by_external_actor(
    tmp_path: Path,
) -> None:
    target = tmp_path / 'tool'
    abandoned_staging = tmp_path / '.tool-stage-abandoned'
    _write(abandoned_staging / 'new.txt', b'not moved yet')
    abandoned = DirectorySwap(abandoned_staging, target)
    abandoned.had_target = False
    abandoned._write_phase('prepared')

    _write(target / 'external.txt', b'external owner')
    abandoned._recover_previous()

    assert (target / 'external.txt').read_bytes() == b'external owner'
    assert (abandoned_staging / 'new.txt').read_bytes() == b'not moved yet'
    assert not abandoned.journal_path.exists()


def test_prepared_directory_swap_reports_missing_original(tmp_path: Path) -> None:
    target = tmp_path / 'tool'
    staging = tmp_path / '.tool-stage'
    _write(staging / 'new.txt', b'new')
    swap = DirectorySwap(staging, target)
    swap.had_target = True
    swap._write_phase('prepared')

    with pytest.raises(RollbackError, match='disappeared'):
        swap._recover_previous()

    assert swap.journal_path.exists()
    assert (staging / 'new.txt').read_bytes() == b'new'


def test_directory_swap_recovery_preserves_external_target(tmp_path: Path) -> None:
    target = tmp_path / 'tool'
    staging = tmp_path / '.tool-stage'
    _write(target / 'old.txt', b'old')
    _write(staging / 'new.txt', b'new')
    swap = DirectorySwap(staging, target)
    swap.had_target = True
    swap._write_phase('prepared')
    os.replace(target, swap.rollback)
    swap._write_phase('old-moved')
    _write(target / 'external.txt', b'external')

    with pytest.raises(OwnershipError, match='external actor'):
        swap._recover_previous()

    assert (target / 'external.txt').read_bytes() == b'external'
    assert (swap.rollback / 'old.txt').read_bytes() == b'old'
    assert swap.journal_path.exists()
