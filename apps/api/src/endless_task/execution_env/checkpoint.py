"""Content-addressed workspace checkpoints and guarded restore (M3B AP-305).

- ``create_checkpoint`` snapshots a workspace: per-file sha256 manifest plus
  a content-addressed blob store (blobs keyed by content hash),
- ``plan_restore`` / ``apply_restore`` revert files changed since the
  checkpoint without ever overwriting **user** modifications: a file whose
  current content differs from the manifest and has no agent-ledger record
  is treated as user-modified and skipped (doc 05 exit gate),
- files created after the checkpoint are left untouched.

Restore never guesses: every decision (apply / skip user change / skip new
file) is reported so callers can show a dry-run plan before applying.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from endless_task.agent_platform import AgentPlatformError, require_identifier

from .ledger import FileMutationLedger


@dataclass(frozen=True)
class CheckpointManifest:
    checkpoint_id: str
    root_fingerprint: str
    entries: tuple[tuple[str, str], ...]  # (relative path, sha256)


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_manifest_json(entries: Sequence[tuple[str, str]]) -> str:
    rows = [
        {"path": path, "sha256": digest}
        for path, digest in sorted(entries, key=lambda item: item[0])
    ]
    return json.dumps(
        rows,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _snapshot_entries(root: Path, *, skip_prefixes: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    entries: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(relative.startswith(prefix) for prefix in skip_prefixes):
            continue
        entries.append((relative, _sha256_bytes(path.read_bytes())))
    return tuple(entries)


def create_checkpoint(
    root: Path,
    store: Path,
    *,
    checkpoint_id: str,
    skip_prefixes: tuple[str, ...] = (),
) -> CheckpointManifest:
    """Snapshot ``root`` into a content-addressed store under ``store``."""
    normalized_id = require_identifier(checkpoint_id, field_name="checkpoint_id")
    root_path = Path(root).expanduser().resolve(strict=False)
    if not root_path.is_dir():
        raise AgentPlatformError(
            "invalid_checkpoint_root",
            "Checkpoint 根目录不可用。",
            retryable=False,
        )
    store_path = Path(store).expanduser().resolve(strict=False)
    store_path.mkdir(parents=True, exist_ok=True)
    entries = _snapshot_entries(root_path, skip_prefixes=tuple(skip_prefixes))
    fingerprint = _sha256_bytes(_canonical_manifest_json(entries).encode("utf-8"))
    blobs = store_path / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    for relative, digest in entries:
        blob_path = blobs / digest
        if not blob_path.exists():
            source = root_path / relative
            blob_path.write_bytes(source.read_bytes())
    manifests = store_path / "manifests"
    manifests.mkdir(parents=True, exist_ok=True)
    manifest_path = manifests / f"{normalized_id}.json"
    manifest_path.write_text(
        _canonical_manifest_json(entries),
        encoding="utf-8",
    )
    return CheckpointManifest(
        checkpoint_id=normalized_id,
        root_fingerprint=fingerprint,
        entries=entries,
    )


def read_manifest(
    store: Path,
    checkpoint_id: str,
) -> CheckpointManifest:
    normalized_id = require_identifier(checkpoint_id, field_name="checkpoint_id")
    manifest_path = Path(store).expanduser() / "manifests" / f"{normalized_id}.json"
    if not manifest_path.exists():
        raise AgentPlatformError(
            "checkpoint_missing",
            "Checkpoint manifest 不存在。",
            retryable=False,
        )
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AgentPlatformError(
            "checkpoint_corrupt",
            "Checkpoint manifest 损坏。",
            retryable=False,
        ) from error
    entries = tuple(
        (require_identifier(item["path"], field_name="path"), item["sha256"])
        for item in raw
    )
    fingerprint = _sha256_bytes(_canonical_manifest_json(entries).encode("utf-8"))
    return CheckpointManifest(
        checkpoint_id=normalized_id,
        root_fingerprint=fingerprint,
        entries=entries,
    )


@dataclass(frozen=True)
class RestorePlan:
    would_apply: tuple[str, ...]
    user_modified_skipped: tuple[str, ...]
    new_files_untouched: tuple[str, ...]
    missing_blobs: tuple[str, ...] = ()

    @property
    def changed_count(self) -> int:
        return len(self.would_apply)


def _current_sha(root: Path, relative: str) -> Optional[str]:
    candidate = root / relative
    if not candidate.exists():
        return None
    if candidate.is_dir():
        return None
    return _sha256_bytes(candidate.read_bytes())


def plan_restore(
    manifest: CheckpointManifest,
    root: Path,
    *,
    ledger: Optional[FileMutationLedger] = None,
) -> RestorePlan:
    root_path = Path(root).expanduser().resolve(strict=False)
    manifest_paths = {relative for relative, _ in manifest.entries}
    would_apply: list[str] = []
    user_modified: list[str] = []
    for relative, checkpoint_sha in sorted(manifest.entries):
        current = _current_sha(root_path, relative)
        if current == checkpoint_sha:
            continue
        if current is None:
            # File missing: restore the checkpoint blob. From hashes alone we
            # cannot distinguish agent vs user deletion, and 05 semantics
            # (test_execution_env_checkpoint) recreate removed files.
            would_apply.append(relative)
            continue
        # Changed and present. Only an agent mutation whose recorded
        # after_hash still matches the current content is revertible; if the
        # content moved on after the last recorded agent write (user edit,
        # another run, any unmediated change) the file is user-owned now and
        # must never be overwritten (G1 restore gate).
        agent_entry = ledger.last_entry_for(relative) if ledger is not None else None
        agent_intact = (
            agent_entry is not None
            and agent_entry.after_hash is not None
            and agent_entry.after_hash == current
        )
        if not agent_intact:
            user_modified.append(relative)
            continue
        would_apply.append(relative)
    current_files = {
        path.relative_to(root_path).as_posix()
        for path in root_path.rglob("*")
        if path.is_file()
    }
    new_files = sorted(current_files - manifest_paths)
    return RestorePlan(
        would_apply=tuple(would_apply),
        user_modified_skipped=tuple(user_modified),
        new_files_untouched=tuple(new_files),
    )


def apply_restore(
    manifest: CheckpointManifest,
    root: Path,
    store: Path,
    *,
    ledger: Optional[FileMutationLedger] = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Apply the dry-run plan; returns (restored_paths, skipped_paths)."""
    plan = plan_restore(manifest, root, ledger=ledger)
    if plan.missing_blobs:
        raise AgentPlatformError(
            "checkpoint_blobs_missing",
            "Checkpoint 内容缺失，拒绝恢复。",
            retryable=False,
        )
    restored: list[str] = []
    skipped: list[str] = []
    for relative in plan.would_apply:
        blob = Path(store).expanduser() / "blobs" / dict(manifest.entries)[relative]
        if not blob.exists():
            skipped.append(relative)
            continue
        target = Path(root).expanduser() / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(blob.read_bytes())
        restored.append(relative)
    skipped.extend(plan.user_modified_skipped)
    skipped.extend(plan.new_files_untouched)
    return tuple(restored), tuple(skipped)


__all__ = [
    "CheckpointManifest",
    "RestorePlan",
    "apply_restore",
    "create_checkpoint",
    "plan_restore",
    "read_manifest",
]
