from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Optional, Protocol

from endless_task.agent_platform import (
    AgentPlatformError,
    EffectReceipt,
    require_identifier,
    require_protocol_version,
)
from endless_task.runtime_ledger import TraceContext

EXECUTION_PROTOCOL_VERSION = 1


class NetworkMode(str, Enum):
    DENY = "deny"
    ALLOW_ALL = "allow_all"
    ALLOW_HOSTS = "allow_hosts"


class FileMutationOperation(str, Enum):
    WRITE = "write"
    DELETE = "delete"


@dataclass(frozen=True)
class ExecutionPolicy:
    workspace_root: str
    read_allow_paths: tuple[str, ...]
    write_allow_paths: tuple[str, ...]
    network_mode: NetworkMode = NetworkMode.DENY
    allowed_hosts: tuple[str, ...] = ()
    environment_allowlist: frozenset[str] = frozenset()
    credential_handles: tuple[str, ...] = ()
    timeout_seconds: float = 30.0
    max_output_characters: int = 200_000
    approval_decision_ref: Optional[str] = None
    protocol_version: int = EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "execution_policy")
        object.__setattr__(
            self,
            "workspace_root",
            require_identifier(self.workspace_root, field_name="workspace_root", max_length=4_096),
        )
        for field_name in ("read_allow_paths", "write_allow_paths", "allowed_hosts", "credential_handles"):
            object.__setattr__(
                self,
                field_name,
                tuple(
                    require_identifier(value, field_name=field_name, max_length=4_096)
                    for value in getattr(self, field_name)
                ),
            )
        object.__setattr__(
            self,
            "environment_allowlist",
            frozenset(
                require_identifier(value, field_name="environment_allowlist")
                for value in self.environment_allowlist
            ),
        )
        if not isinstance(self.network_mode, NetworkMode):
            raise AgentPlatformError("invalid_execution_value", "Network mode must use a protocol enum value")
        if self.network_mode is NetworkMode.ALLOW_HOSTS and not self.allowed_hosts:
            raise AgentPlatformError("invalid_execution_value", "allow_hosts network mode requires at least one host")
        if self.network_mode is not NetworkMode.ALLOW_HOSTS and self.allowed_hosts:
            raise AgentPlatformError("invalid_execution_value", "allowed_hosts is only valid with allow_hosts network mode")
        if not 0 < self.timeout_seconds <= 86_400:
            raise AgentPlatformError("invalid_execution_value", "Execution timeout must be within (0, 86400]")
        if self.max_output_characters <= 0:
            raise AgentPlatformError("invalid_execution_value", "Execution output limit must be positive")
        if self.approval_decision_ref is not None:
            object.__setattr__(
                self,
                "approval_decision_ref",
                require_identifier(
                    self.approval_decision_ref,
                    field_name="approval_decision_ref",
                ),
            )


@dataclass(frozen=True)
class ReadFileRequest:
    path: str
    policy: ExecutionPolicy
    trace: TraceContext
    encoding: str = "utf-8"
    protocol_version: int = EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "read_file_request")
        for field_name in ("path", "encoding"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name, max_length=4_096),
            )


@dataclass(frozen=True)
class FileReadResult:
    path: str
    content: str
    content_hash: str
    truncated: bool = False
    protocol_version: int = EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "file_read_result")
        for field_name in ("path", "content_hash"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name, max_length=4_096),
            )
        if not isinstance(self.content, str) or not isinstance(self.truncated, bool):
            raise AgentPlatformError("invalid_execution_value", "File read content and flags have invalid types")


@dataclass(frozen=True)
class FileMutationRequest:
    effect_id: str
    tool_call_id: str
    path: str
    operation: FileMutationOperation
    policy: ExecutionPolicy
    trace: TraceContext
    requested_at: str
    content: Optional[str] = None
    expected_before_hash: Optional[str] = None
    idempotency_key: Optional[str] = None
    protocol_version: int = EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "file_mutation_request")
        for field_name in ("effect_id", "tool_call_id", "path", "requested_at"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name, max_length=4_096),
            )
        if not isinstance(self.operation, FileMutationOperation):
            raise AgentPlatformError("invalid_execution_value", "File mutation operation must use a protocol enum value")
        if self.operation is FileMutationOperation.WRITE and self.content is None:
            raise AgentPlatformError("invalid_execution_value", "Write mutations require content")
        if self.operation is FileMutationOperation.DELETE and self.content is not None:
            raise AgentPlatformError("invalid_execution_value", "Delete mutations cannot carry content")
        for field_name in ("expected_before_hash", "idempotency_key"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    require_identifier(value, field_name=field_name),
                )


@dataclass(frozen=True)
class ProcessRequest:
    effect_id: str
    tool_call_id: str
    argv: tuple[str, ...]
    cwd: str
    policy: ExecutionPolicy
    trace: TraceContext
    requested_at: str
    environment: Mapping[str, str] = field(default_factory=dict)
    stdin: Optional[str] = None
    idempotency_key: Optional[str] = None
    protocol_version: int = EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "process_request")
        for field_name in ("effect_id", "tool_call_id", "cwd", "requested_at"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name, max_length=4_096),
            )
        if not self.argv:
            raise AgentPlatformError("invalid_execution_value", "Process argv cannot be empty")
        object.__setattr__(
            self,
            "argv",
            tuple(require_identifier(value, field_name="argv", max_length=32_768) for value in self.argv),
        )
        copied_environment: dict[str, str] = {}
        for key, value in self.environment.items():
            copied_environment[
                require_identifier(key, field_name="environment_key")
            ] = require_identifier(value, field_name="environment_value", max_length=32_768)
        object.__setattr__(self, "environment", copied_environment)


@dataclass(frozen=True)
class ProcessResult:
    exit_code: Optional[int]
    stdout: str
    stderr: str
    receipt: EffectReceipt
    truncated: bool = False
    protocol_version: int = EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "process_result")
        if self.exit_code is not None and not isinstance(self.exit_code, int):
            raise AgentPlatformError("invalid_execution_value", "Process exit code must be an integer when known")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise AgentPlatformError("invalid_execution_value", "Process output must be text")
        if not isinstance(self.truncated, bool):
            raise AgentPlatformError("invalid_execution_value", "Process truncated flag must be boolean")


@dataclass(frozen=True)
class CheckpointRequest:
    run_id: str
    policy: ExecutionPolicy
    trace: TraceContext
    created_at: str
    protocol_version: int = EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "checkpoint_request")
        for field_name in ("run_id", "created_at"):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name),
            )
        if self.run_id != self.trace.run_id:
            raise AgentPlatformError("invalid_execution_value", "Checkpoint request and trace must reference the same run")


@dataclass(frozen=True)
class CheckpointRef:
    checkpoint_id: str
    run_id: str
    backend: str
    root_fingerprint: str
    manifest_ref: str
    created_at: str
    protocol_version: int = EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "checkpoint_ref")
        for field_name in (
            "checkpoint_id",
            "run_id",
            "backend",
            "root_fingerprint",
            "manifest_ref",
            "created_at",
        ):
            object.__setattr__(
                self,
                field_name,
                require_identifier(getattr(self, field_name), field_name=field_name, max_length=4_096),
            )


@dataclass(frozen=True)
class RestoreRequest:
    checkpoint: CheckpointRef
    policy: ExecutionPolicy
    trace: TraceContext
    dry_run: bool = True
    protocol_version: int = EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "restore_request")
        if not isinstance(self.dry_run, bool):
            raise AgentPlatformError("invalid_execution_value", "Restore dry_run flag must be boolean")


@dataclass(frozen=True)
class RestoreResult:
    applied: bool
    restored_paths: tuple[str, ...] = ()
    skipped_paths: tuple[str, ...] = ()
    protocol_version: int = EXECUTION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _validate_version(self.protocol_version, "restore_result")
        if not isinstance(self.applied, bool):
            raise AgentPlatformError("invalid_execution_value", "Restore applied flag must be boolean")
        for field_name in ("restored_paths", "skipped_paths"):
            object.__setattr__(
                self,
                field_name,
                tuple(
                    require_identifier(value, field_name=field_name, max_length=4_096)
                    for value in getattr(self, field_name)
                ),
            )


class ExecutionEnvironment(Protocol):
    async def read_file(self, request: ReadFileRequest) -> FileReadResult: ...

    async def mutate_file(self, request: FileMutationRequest) -> EffectReceipt: ...

    async def run_process(self, request: ProcessRequest) -> ProcessResult: ...

    async def checkpoint(self, request: CheckpointRequest) -> CheckpointRef: ...

    async def restore(self, request: RestoreRequest) -> RestoreResult: ...


def _validate_version(version: int, protocol: str) -> None:
    require_protocol_version(version, expected=EXECUTION_PROTOCOL_VERSION, protocol=protocol)