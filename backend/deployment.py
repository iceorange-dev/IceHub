"""Transactional deployment helpers with ownership tracking.

The application modifies executables and DLLs owned by other programs.  Those
changes must either complete fully or restore the exact state that existed
before the operation.  This module keeps the first observed original forever
and records hashes for every managed target.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
from typing import Any
import uuid

from .security import UnsafeArchiveError, normalize_zip_member, sha256_file
from .storage import FileLock
from .platform.files import _is_link_or_reparse


class DeploymentError(RuntimeError):
    """Base class for deployment failures."""


class OwnershipError(DeploymentError):
    """Raised when a target no longer matches the file IceHub deployed."""


class RollbackError(DeploymentError):
    """Raised when both an operation and its rollback fail."""


@dataclass(frozen=True)
class DeploymentResult:
    product: str
    version: str
    target_root: Path
    changed_files: tuple[Path, ...]
    manifest_path: Path


_PROCESS_LOCK = threading.RLock()


def _safe_component(value: str, *, field: str) -> str:
    if not value or value in {'.', '..'}:
        raise DeploymentError(f'{field} must not be empty')
    try:
        normalized = normalize_zip_member(value)
    except UnsafeArchiveError as error:
        raise DeploymentError(f'{field} is not a safe path component') from error
    if len(normalized.parts) != 1 or normalized.name != value:
        raise DeploymentError(f'{field} must be a single canonical path component')
    return value


def _target_key(target_root: Path) -> str:
    normalized = os.path.normcase(str(target_root.resolve(strict=False)))
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:20]


def _atomic_json_write(path: Path, data: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.{uuid.uuid4().hex}.tmp')
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_fsync(source: Path, destination: Path) -> None:
    if _is_link_or_reparse(source) or not source.is_file():
        raise OwnershipError(f'refusing to copy a link or non-file: {source}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open('rb') as src, destination.open('xb') as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())
    shutil.copystat(source, destination, follow_symlinks=False)


def _replace_from(source: Path, target: Path, *, transaction_id: str) -> None:
    temporary = target.with_name(f'.{target.name}.icehub-{transaction_id}.tmp')
    temporary.unlink(missing_ok=True)
    try:
        _copy_fsync(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open('r', encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise DeploymentError(f'ownership manifest is unreadable: {path}') from error
    if not isinstance(data, dict) or data.get('schema') != 1:
        raise DeploymentError(f'unsupported ownership manifest: {path}')
    return data


def _load_json_object(path: Path, *, label: str) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open('r', encoding='utf-8') as stream:
            data = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise DeploymentError(f'{label} is unreadable: {path}') from error
    if not isinstance(data, dict):
        raise DeploymentError(f'{label} must be a JSON object: {path}')
    return data


def _is_sha256(value: Any) -> bool:
    text = str(value)
    return len(text) == 64 and all(character in '0123456789abcdef' for character in text)


def _remove_owned_path(path: Path) -> None:
    """Remove a path that was moved into an IceHub-owned quarantine.

    Reparse points are never traversed.  Unexpected links are left in place so
    a later/manual cleanup is safer than deleting through an attacker-controlled
    target.
    """

    if not path.exists() and not path.is_symlink():
        return
    if _is_link_or_reparse(path):
        raise OwnershipError(f'refusing to recursively remove a link: {path}')
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _directory_digest(root: Path) -> str:
    """Hash a directory's relative file names and contents without following links."""

    if _is_link_or_reparse(root) or not root.is_dir():
        raise OwnershipError(f'directory identity root is unsafe: {root}')
    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob('*') if path.is_file()),
        key=lambda path: str(path.relative_to(root)).replace('\\', '/').casefold(),
    )
    for path in files:
        if _is_link_or_reparse(path):
            raise OwnershipError(f'directory identity contains a link: {path}')
        relative = str(path.relative_to(root)).replace('\\', '/')
        digest.update(relative.encode('utf-8'))
        digest.update(b'\0')
        digest.update(sha256_file(path).encode('ascii'))
        digest.update(b'\0')
    return digest.hexdigest()


class ManagedFileDeployment:
    """Install and uninstall a set of files as one transaction."""

    def __init__(self, state_root: Path, product: str, target_root: Path):
        self.state_root = Path(state_root).resolve(strict=False)
        self.product = _safe_component(product, field='product')
        raw_target = Path(target_root).absolute()
        if raw_target.exists() and _is_link_or_reparse(raw_target):
            raise OwnershipError(f'refusing to manage a linked target root: {raw_target}')
        self.target_root = raw_target.resolve(strict=False)
        key = _target_key(self.target_root)
        self.manifest_path = self.state_root / 'manifests' / f'{product}-{key}.json'
        self.backup_root = self.state_root / 'backups' / f'{product}-{key}'
        self.lock_path = self.state_root / 'locks' / f'{product}-{key}.lock'
        self.journal_path = self.state_root / 'journals' / f'{product}-{key}.json'
        self.transaction_root = self.state_root / 'transactions' / f'{product}-{key}'

    def _validate_source_files(self, files: Mapping[str, Path]) -> dict[str, Path]:
        if not files:
            raise DeploymentError('at least one file is required')
        validated: dict[str, Path] = {}
        seen_names: set[str] = set()
        for raw_name, raw_source in files.items():
            name = _safe_component(str(raw_name), field='target name')
            key = os.path.normcase(name).casefold()
            if key in seen_names:
                raise DeploymentError('target names collide on a case-insensitive filesystem')
            seen_names.add(key)
            lexical_source = Path(raw_source).absolute()
            if _is_link_or_reparse(lexical_source):
                raise DeploymentError(f'deployment source is a link: {lexical_source}')
            source = lexical_source.resolve(strict=True)
            if not source.is_file():
                raise DeploymentError(f'deployment source is not a regular file: {source}')
            validated[name] = source
        return validated

    def _verify_manifest_identity(self, manifest: Mapping[str, Any]) -> None:
        if manifest.get('product') != self.product:
            raise DeploymentError('ownership manifest belongs to another product')
        recorded_root = Path(str(manifest.get('target_root', ''))).resolve(strict=False)
        if os.path.normcase(str(recorded_root)) != os.path.normcase(str(self.target_root)):
            raise DeploymentError('ownership manifest belongs to another target')
        if not isinstance(manifest.get('files'), dict):
            raise DeploymentError('ownership manifest has invalid file records')
        deployment_id = str(manifest.get('deployment_id', ''))
        try:
            if uuid.UUID(deployment_id).hex != deployment_id:
                raise ValueError
        except ValueError as error:
            raise DeploymentError('ownership manifest has an invalid deployment id') from error
        expected_backup_dir = (
            self.backup_root / deployment_id / 'original'
        ).resolve(strict=False)
        seen_names: set[str] = set()
        for name, record in manifest['files'].items():
            _safe_component(str(name), field='manifest target name')
            normalized_name = os.path.normcase(str(name)).casefold()
            if normalized_name in seen_names:
                raise DeploymentError('ownership manifest contains colliding target names')
            seen_names.add(normalized_name)
            if not isinstance(record, dict):
                raise DeploymentError('ownership manifest has an invalid file record')
            if not isinstance(record.get('original_existed'), bool):
                raise DeploymentError('ownership manifest has an invalid original flag')
            deployed_hash = record.get('deployed_sha256')
            if not _is_sha256(deployed_hash):
                raise DeploymentError('ownership manifest has an invalid deployed hash')
            if record['original_existed']:
                if not _is_sha256(record.get('original_sha256')):
                    raise DeploymentError('ownership manifest has an invalid original hash')
                backup = Path(str(record.get('original_backup', ''))).resolve(strict=False)
                expected = (expected_backup_dir / str(name)).resolve(strict=False)
                if backup != expected:
                    raise DeploymentError(
                        'ownership manifest backup path escapes its deployment'
                    )

    def _snapshot_targets(self, names: list[str], transaction: Path) -> dict[str, Path | None]:
        snapshots: dict[str, Path | None] = {}
        for name in names:
            target = self.target_root / name
            if _is_link_or_reparse(target):
                raise OwnershipError(f'refusing to manage symbolic link: {target}')
            if target.exists():
                if not target.is_file():
                    raise OwnershipError(f'refusing to manage a non-file target: {target}')
                snapshot = transaction / 'current' / name
                _copy_fsync(target, snapshot)
                snapshots[name] = snapshot
            else:
                snapshots[name] = None
        return snapshots

    def _rollback_targets(
        self,
        snapshots: Mapping[str, Path | None],
        *,
        transaction_id: str,
    ) -> None:
        errors: list[str] = []
        for name, snapshot in snapshots.items():
            target = self.target_root / name
            try:
                if snapshot is None:
                    target.unlink(missing_ok=True)
                else:
                    _replace_from(snapshot, target, transaction_id=transaction_id)
            except Exception as error:  # pragma: no cover - catastrophic filesystem failure
                errors.append(f'{name}: {error}')
        if errors:
            raise RollbackError('rollback failed: ' + '; '.join(errors))

    def _transaction_path(self, transaction_id: str) -> Path:
        try:
            if uuid.UUID(transaction_id).hex != transaction_id:
                raise ValueError
        except ValueError as error:
            raise DeploymentError('transaction journal has an invalid id') from error
        transaction = (self.transaction_root / transaction_id).resolve(strict=False)
        if transaction.parent != self.transaction_root.resolve(strict=False):
            raise DeploymentError('transaction journal escapes its state directory')
        return transaction

    def _validate_journal(self, journal: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
        if journal.get('schema') != 1 or journal.get('kind') != 'managed-files':
            raise DeploymentError('unsupported deployment transaction journal')
        if journal.get('product') != self.product:
            raise DeploymentError('deployment journal belongs to another product')
        recorded_root = Path(str(journal.get('target_root', ''))).resolve(strict=False)
        if os.path.normcase(str(recorded_root)) != os.path.normcase(str(self.target_root)):
            raise DeploymentError('deployment journal belongs to another target')
        transaction = self._transaction_path(str(journal.get('transaction_id', '')))
        file_records = journal.get('files')
        if not isinstance(file_records, dict) or not file_records:
            raise DeploymentError('deployment journal has invalid file records')
        seen_names: set[str] = set()
        validated: dict[str, Any] = {}
        for raw_name, raw_record in file_records.items():
            name = _safe_component(str(raw_name), field='journal target name')
            folded = os.path.normcase(name).casefold()
            if folded in seen_names:
                raise DeploymentError('deployment journal contains colliding target names')
            seen_names.add(folded)
            if not isinstance(raw_record, dict) or not isinstance(raw_record.get('existed'), bool):
                raise DeploymentError('deployment journal has an invalid target snapshot')
            validated[name] = {'existed': raw_record['existed']}
        if not isinstance(journal.get('previous_manifest'), bool):
            raise DeploymentError('deployment journal has an invalid manifest snapshot flag')
        if journal.get('phase') not in {'prepared', 'committed'}:
            raise DeploymentError('deployment journal has an invalid phase')
        return transaction, validated

    def _prepare_transaction(
        self,
        names: list[str],
        *,
        operation: str,
        transaction_id: str,
    ) -> tuple[Path, dict[str, Path | None]]:
        transaction = self._transaction_path(transaction_id)
        transaction.mkdir(parents=True, exist_ok=False)
        try:
            snapshots = self._snapshot_targets(names, transaction)
            previous_manifest = self.manifest_path.exists()
            if previous_manifest:
                _copy_fsync(self.manifest_path, transaction / 'manifest.json')
            journal = {
                'schema': 1,
                'kind': 'managed-files',
                'product': self.product,
                'target_root': str(self.target_root),
                'transaction_id': transaction_id,
                'operation': operation,
                'phase': 'prepared',
                'previous_manifest': previous_manifest,
                'files': {
                    name: {'existed': snapshot is not None}
                    for name, snapshot in snapshots.items()
                },
            }
            _atomic_json_write(self.journal_path, journal)
            return transaction, snapshots
        except BaseException:
            if not self.journal_path.exists():
                shutil.rmtree(transaction, ignore_errors=True)
            raise

    def _recover_incomplete_transaction(self) -> None:
        journal = _load_json_object(self.journal_path, label='deployment transaction journal')
        if journal is None:
            return
        transaction, files = self._validate_journal(journal)
        if journal['phase'] == 'committed':
            shutil.rmtree(transaction, ignore_errors=True)
            self.journal_path.unlink(missing_ok=True)
            return
        if not transaction.is_dir() or _is_link_or_reparse(transaction):
            raise RollbackError('deployment transaction snapshot is missing or unsafe')

        errors: list[str] = []
        transaction_id = str(journal['transaction_id'])
        for name, record in files.items():
            target = self.target_root / name
            try:
                if _is_link_or_reparse(target):
                    raise OwnershipError(f'refusing to replace a linked target: {target}')
                if record['existed']:
                    snapshot = transaction / 'current' / name
                    if not snapshot.is_file() or _is_link_or_reparse(snapshot):
                        raise RollbackError(f'target snapshot is missing or unsafe: {snapshot}')
                    _replace_from(snapshot, target, transaction_id=transaction_id)
                else:
                    target.unlink(missing_ok=True)
            except Exception as error:  # pragma: no cover - catastrophic filesystem failure
                errors.append(f'{name}: {error}')

        try:
            if journal['previous_manifest']:
                snapshot = transaction / 'manifest.json'
                if not snapshot.is_file() or _is_link_or_reparse(snapshot):
                    raise RollbackError('ownership manifest snapshot is missing or unsafe')
                _replace_from(snapshot, self.manifest_path, transaction_id=transaction_id)
            else:
                self.manifest_path.unlink(missing_ok=True)
        except Exception as error:  # pragma: no cover - catastrophic filesystem failure
            errors.append(f'manifest: {error}')

        if errors:
            raise RollbackError('transaction recovery failed: ' + '; '.join(errors))
        shutil.rmtree(transaction, ignore_errors=True)
        self.journal_path.unlink(missing_ok=True)

    def _commit_transaction(self, transaction: Path) -> None:
        journal = _load_json_object(self.journal_path, label='deployment transaction journal')
        if journal is None:
            raise DeploymentError('deployment transaction journal disappeared before commit')
        recorded_transaction, _ = self._validate_journal(journal)
        if recorded_transaction != transaction.resolve(strict=False):
            raise DeploymentError('deployment transaction journal changed before commit')
        committed = dict(journal)
        committed['phase'] = 'committed'
        _atomic_json_write(self.journal_path, committed)
        # Cleanup is deliberately post-commit.  A power loss or deletion error
        # leaves a committed journal that the next operation can clean safely.
        shutil.rmtree(transaction, ignore_errors=True)
        try:
            self.journal_path.unlink(missing_ok=True)
        except OSError:
            pass

    def install(
        self,
        files: Mapping[str, Path],
        *,
        version: str = '',
        original_overrides: Mapping[str, Path] | None = None,
        created_targets: Collection[str] = (),
    ) -> DeploymentResult:
        """Deploy files and preserve the first observed originals.

        ``original_overrides`` is used to migrate the legacy ``.ost_backup``
        directory.  Its files become the immutable originals instead of the
        currently deployed legacy OST files.
        """

        sources = self._validate_source_files(files)
        overrides = {str(k): Path(v).absolute() for k, v in (original_overrides or {}).items()}
        if not set(overrides).issubset(sources):
            raise DeploymentError('original_overrides contains an unmanaged file')
        for name, override in overrides.items():
            _safe_component(name, field='override target name')
            if _is_link_or_reparse(override) or not override.is_file():
                raise OwnershipError(f'legacy original is missing, linked, or unsafe: {override}')
        created = {_safe_component(str(name), field='created target') for name in created_targets}
        if not created.issubset(sources):
            raise DeploymentError('created_targets contains an unmanaged file')
        self.target_root.mkdir(parents=True, exist_ok=True)
        transaction_id = uuid.uuid4().hex

        with _PROCESS_LOCK, FileLock(self.lock_path, timeout=30):
            self._recover_incomplete_transaction()
            source_hashes = {name: sha256_file(source) for name, source in sources.items()}
            manifest = _load_manifest(self.manifest_path)
            new_manifest = manifest is None
            if manifest is not None:
                self._verify_manifest_identity(manifest)
                records: dict[str, Any] = dict(manifest['files'])
                if set(records) != set(sources):
                    raise OwnershipError('managed file set changed; explicit migration is required')
                for name, record in records.items():
                    target = self.target_root / name
                    if target.exists():
                        current_hash = sha256_file(target)
                        expected_hash = str(record.get('deployed_sha256', ''))
                        if current_hash != expected_hash:
                            raise OwnershipError(
                                f'{target} was modified outside IceHub; refusing to overwrite it'
                            )
            else:
                deployment_id = uuid.uuid4().hex
                backup_dir = self.backup_root / deployment_id / 'original'
                records = {}
                manifest = {
                    'schema': 1,
                    'product': self.product,
                    'deployment_id': deployment_id,
                    'target_root': str(self.target_root),
                    'created_at': datetime.now(UTC).isoformat(),
                    'files': records,
                }

            transaction: Path | None = None
            try:
                transaction, snapshots = self._prepare_transaction(
                    list(sources), operation='install', transaction_id=transaction_id
                )
                if new_manifest:
                    for name in sources:
                        override = overrides.get(name)
                        original = override or snapshots[name]
                        existed = name not in created and original is not None
                        backup: Path | None = None
                        original_hash = ''
                        if existed:
                            assert original is not None
                            backup = backup_dir / name
                            _copy_fsync(original, backup)
                            original_hash = sha256_file(backup)
                        records[name] = {
                            'original_existed': existed,
                            'original_backup': str(backup) if backup else '',
                            'original_sha256': original_hash,
                            'deployed_sha256': source_hashes[name],
                        }
                else:
                    for name, snapshot in snapshots.items():
                        if snapshot is None or sha256_file(snapshot) != records[name]['deployed_sha256']:
                            raise OwnershipError(
                                f'{self.target_root / name} changed while preparing the update'
                            )

                for name, source in sources.items():
                    _replace_from(source, self.target_root / name, transaction_id=transaction_id)
                    if sha256_file(self.target_root / name) != source_hashes[name]:
                        raise OwnershipError(
                            f'{self.target_root / name} changed during deployment'
                        )
                    records[name]['deployed_sha256'] = source_hashes[name]

                manifest['version'] = str(version)
                manifest['updated_at'] = datetime.now(UTC).isoformat()
                manifest['files'] = records
                _atomic_json_write(self.manifest_path, manifest)
                self._commit_transaction(transaction)
            except Exception as operation_error:
                try:
                    if self.journal_path.exists():
                        self._recover_incomplete_transaction()
                except (DeploymentError, RollbackError) as rollback_error:
                    raise RollbackError(
                        f'install failed ({operation_error}); {rollback_error}'
                    ) from operation_error
                raise

        return DeploymentResult(
            product=self.product,
            version=str(version),
            target_root=self.target_root,
            changed_files=tuple(self.target_root / name for name in sources),
            manifest_path=self.manifest_path,
        )

    def status(self) -> dict[str, Any]:
        """Return ownership and hash status without modifying the target."""

        with _PROCESS_LOCK, FileLock(self.lock_path, timeout=30):
            self._recover_incomplete_transaction()
            manifest = _load_manifest(self.manifest_path)
            if manifest is None:
                return {'managed': False, 'healthy': False, 'version': '', 'files': {}}
            self._verify_manifest_identity(manifest)
            status: dict[str, Any] = {
                'managed': True,
                'healthy': True,
                'version': str(manifest.get('version', '')),
                'files': {},
            }
            for name, record in manifest['files'].items():
                target = self.target_root / name
                present = target.is_file() and not _is_link_or_reparse(target)
                current_hash = sha256_file(target) if present else ''
                expected_hash = str(record.get('deployed_sha256', ''))
                matches = present and current_hash == expected_hash
                status['files'][name] = {
                    'present': present,
                    'matches': matches,
                    'current_sha256': current_hash,
                    'expected_sha256': expected_hash,
                }
                status['healthy'] = bool(status['healthy'] and matches)
            return status

    def uninstall(self) -> DeploymentResult:
        """Restore immutable originals or remove files created by IceHub."""

        transaction_id = uuid.uuid4().hex
        with _PROCESS_LOCK, FileLock(self.lock_path, timeout=30):
            self._recover_incomplete_transaction()
            manifest = _load_manifest(self.manifest_path)
            if manifest is None:
                raise OwnershipError('no IceHub ownership manifest exists for this target')
            self._verify_manifest_identity(manifest)
            records: dict[str, Any] = manifest['files']

            for name, record in records.items():
                _safe_component(name, field='manifest target name')
                target = self.target_root / name
                if _is_link_or_reparse(target):
                    raise OwnershipError(f'refusing to manage symbolic link: {target}')
                if target.exists():
                    current_hash = sha256_file(target)
                    deployed_hash = str(record.get('deployed_sha256', ''))
                    original_hash = str(record.get('original_sha256', ''))
                    if current_hash not in {deployed_hash, original_hash}:
                        raise OwnershipError(
                            f'{target} was modified outside IceHub; refusing to remove it'
                        )

                if record['original_existed']:
                    backup = Path(str(record['original_backup'])).absolute()
                    if _is_link_or_reparse(backup):
                        raise OwnershipError(f'original backup is linked or unsafe: {backup}')
                    if not backup.is_file() or sha256_file(backup) != record['original_sha256']:
                        raise OwnershipError(f'original backup is missing or damaged: {backup}')

            transaction: Path | None = None
            try:
                transaction, snapshots = self._prepare_transaction(
                    list(records), operation='uninstall', transaction_id=transaction_id
                )
                for name, snapshot in snapshots.items():
                    if snapshot is None:
                        continue
                    current_hash = sha256_file(snapshot)
                    record = records[name]
                    if current_hash not in {
                        record['deployed_sha256'],
                        record.get('original_sha256', ''),
                    }:
                        raise OwnershipError(
                            f'{self.target_root / name} changed while preparing uninstall'
                        )
                for name, record in records.items():
                    target = self.target_root / name
                    if record['original_existed']:
                        backup = Path(str(record['original_backup'])).absolute()
                        _replace_from(backup, target, transaction_id=transaction_id)
                        if sha256_file(target) != record['original_sha256']:
                            raise OwnershipError(f'original restore changed unexpectedly: {target}')
                    else:
                        target.unlink(missing_ok=True)
                self.manifest_path.unlink()
                self._commit_transaction(transaction)
            except Exception as operation_error:
                try:
                    if self.journal_path.exists():
                        self._recover_incomplete_transaction()
                except (DeploymentError, RollbackError) as rollback_error:
                    raise RollbackError(
                        f'uninstall failed ({operation_error}); {rollback_error}'
                    ) from operation_error
                raise

            deployment_id = str(manifest.get('deployment_id', ''))
            backup_to_remove = (self.backup_root / deployment_id).resolve(strict=False)
            expected_parent = self.backup_root.resolve(strict=False)
            if backup_to_remove.parent == expected_parent:
                try:
                    _remove_owned_path(backup_to_remove)
                except (OSError, OwnershipError):
                    # Restoration and manifest removal already committed.  A
                    # stale immutable backup is harmless and can be retried or
                    # removed manually; it must not turn success into failure.
                    pass

        return DeploymentResult(
            product=self.product,
            version=str(manifest.get('version', '')),
            target_root=self.target_root,
            changed_files=tuple(self.target_root / name for name in records),
            manifest_path=self.manifest_path,
        )


class DirectorySwap:
    """Atomically swap a validated staging directory into place.

    The caller performs any post-swap checks and calls :meth:`commit`.  Leaving
    the context without committing automatically restores the previous tree.
    """

    def __init__(self, staging: Path, target: Path):
        self.staging = Path(staging).absolute()
        self.target = Path(target).absolute()
        _safe_component(self.target.name, field='swap target name')
        self.transaction_id = uuid.uuid4().hex
        self.rollback = self.target.parent / (
            f'.{self.target.name}.icehub-old-{self.transaction_id}'
        )
        self.quarantine = self.target.parent / (
            f'.{self.target.name}.icehub-new-{self.transaction_id}'
        )
        self.journal_path = self.target.parent / f'.{self.target.name}.icehub-swap.json'
        self.lock_path = self.target.parent / f'.{self.target.name}.icehub-swap.lock'
        self.had_target = False
        self.committed = False
        self.staging_digest = ''

    def _paths_for(self, transaction_id: str) -> tuple[Path, Path]:
        try:
            if uuid.UUID(transaction_id).hex != transaction_id:
                raise ValueError
        except ValueError as error:
            raise DeploymentError('directory swap journal has an invalid id') from error
        rollback = self.target.parent / f'.{self.target.name}.icehub-old-{transaction_id}'
        quarantine = self.target.parent / f'.{self.target.name}.icehub-new-{transaction_id}'
        return rollback, quarantine

    def _read_journal(self) -> dict[str, Any] | None:
        journal = _load_json_object(self.journal_path, label='directory swap journal')
        if journal is None:
            return None
        if journal.get('schema') != 1 or journal.get('kind') != 'directory-swap':
            raise DeploymentError('unsupported directory swap journal')
        recorded_target = Path(str(journal.get('target', ''))).absolute()
        if os.path.normcase(str(recorded_target)) != os.path.normcase(str(self.target)):
            raise DeploymentError('directory swap journal belongs to another target')
        self._paths_for(str(journal.get('transaction_id', '')))
        if not isinstance(journal.get('had_target'), bool):
            raise DeploymentError('directory swap journal has an invalid target flag')
        if not _is_sha256(journal.get('staging_digest')):
            raise DeploymentError('directory swap journal has an invalid staging digest')
        if journal.get('phase') not in {'prepared', 'old-moved', 'new-moved', 'committed'}:
            raise DeploymentError('directory swap journal has an invalid phase')
        return journal

    def _write_phase(self, phase: str) -> None:
        if not self.staging_digest:
            self.staging_digest = _directory_digest(self.staging)
        _atomic_json_write(
            self.journal_path,
            {
                'schema': 1,
                'kind': 'directory-swap',
                'target': str(self.target),
                'transaction_id': self.transaction_id,
                'had_target': self.had_target,
                'staging_digest': self.staging_digest,
                'phase': phase,
            },
        )

    @staticmethod
    def _exists(path: Path) -> bool:
        return path.exists() or path.is_symlink()

    def _cleanup_best_effort(self, path: Path) -> bool:
        try:
            _remove_owned_path(path)
            return True
        except (OSError, OwnershipError):
            return False

    def _recover_previous(self) -> None:
        journal = self._read_journal()
        if journal is None:
            return
        transaction_id = str(journal['transaction_id'])
        rollback, quarantine = self._paths_for(transaction_id)
        phase = str(journal['phase'])
        had_target = bool(journal['had_target'])
        rollback_exists = self._exists(rollback)
        target_exists = self._exists(self.target)

        if phase == 'committed':
            if had_target and not target_exists:
                if rollback_exists:
                    os.replace(rollback, self.target)
                    self.journal_path.unlink(missing_ok=True)
                    return
                raise RollbackError('committed directory swap target and original are both missing')
            if target_exists:
                if _directory_digest(self.target) != journal['staging_digest']:
                    raise OwnershipError(
                        'committed directory swap target changed before old-tree cleanup'
                    )
            old_clean = self._cleanup_best_effort(rollback)
            new_clean = self._cleanup_best_effort(quarantine)
            if old_clean and new_clean:
                self.journal_path.unlink(missing_ok=True)
            return

        if phase == 'prepared' and not rollback_exists:
            # No rename performed by this transaction can have completed yet:
            # when an original exists its rename creates ``rollback`` first,
            # and when it does not, the journal advances to ``old-moved``
            # before staging is renamed.  A target appearing now belongs to an
            # external actor and must be preserved.
            if had_target and not target_exists:
                raise RollbackError(
                    'directory swap target disappeared before its original was journaled'
                )
            self.journal_path.unlink(missing_ok=True)
            return
        if rollback_exists:
            if target_exists:
                if _directory_digest(self.target) != journal['staging_digest']:
                    raise OwnershipError(
                        'directory swap target was created or changed by an external actor'
                    )
                if self._exists(quarantine):
                    raise RollbackError('directory swap recovery quarantine is occupied')
                os.replace(self.target, quarantine)
            os.replace(rollback, self.target)
        elif had_target:
            # A prepared journal can exist before the original directory was
            # moved.  In every later phase, a missing rollback is unrecoverable.
            if phase != 'prepared' or not target_exists:
                raise RollbackError('directory swap original directory is missing')
        elif target_exists:
            if _directory_digest(self.target) != journal['staging_digest']:
                raise OwnershipError(
                    'directory swap target was created or changed by an external actor'
                )
            if self._exists(quarantine):
                raise RollbackError('directory swap recovery quarantine is occupied')
            os.replace(self.target, quarantine)

        self.journal_path.unlink(missing_ok=True)
        self._cleanup_best_effort(quarantine)

    def __enter__(self) -> DirectorySwap:
        with _PROCESS_LOCK, FileLock(self.lock_path, timeout=30):
            self._recover_previous()
            if _is_link_or_reparse(self.staging) or not self.staging.is_dir():
                raise DeploymentError('staging path must be a regular directory')
            if self.staging.parent.absolute() != self.target.parent.absolute():
                raise DeploymentError('staging and target must share a parent for an atomic swap')
            self.staging_digest = _directory_digest(self.staging)
            self.had_target = self._exists(self.target)
            if self.had_target and _is_link_or_reparse(self.target):
                raise OwnershipError(f'refusing to replace linked target: {self.target}')
            self._write_phase('prepared')
            try:
                if self.had_target:
                    os.replace(self.target, self.rollback)
                self._write_phase('old-moved')
                os.replace(self.staging, self.target)
                self._write_phase('new-moved')
            except Exception:
                self._recover_previous()
                raise
        return self

    def commit(self) -> None:
        with _PROCESS_LOCK, FileLock(self.lock_path, timeout=30):
            journal = self._read_journal()
            if journal is None or journal.get('transaction_id') != self.transaction_id:
                raise DeploymentError('directory swap transaction disappeared before commit')
            if journal.get('phase') != 'new-moved':
                raise DeploymentError('directory swap is not ready to commit')
            self._write_phase('committed')
            self.committed = True

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        with _PROCESS_LOCK, FileLock(self.lock_path, timeout=30):
            self._recover_previous()
        return False


def validate_directory_tree(root: Path, validator: Callable[[Path], None]) -> None:
    """Run a caller supplied validator for every regular file in a tree."""

    resolved_root = Path(root).resolve(strict=True)
    for path in resolved_root.rglob('*'):
        if path.is_symlink():
            raise OwnershipError(f'directory tree contains a symbolic link: {path}')
        if path.is_file():
            validator(path)


__all__ = [
    'DeploymentError',
    'DeploymentResult',
    'DirectorySwap',
    'ManagedFileDeployment',
    'OwnershipError',
    'RollbackError',
    'validate_directory_tree',
]
