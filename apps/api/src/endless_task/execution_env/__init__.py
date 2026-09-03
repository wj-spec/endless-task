"""Replaceable execution and side-effect backend contracts."""

from .conformance import assert_execution_environment_conformance
from .fakes import FakeExecutionEnvironment
from .local import LocalExecutionBackend
from .seatbelt import SeatbeltBackend, build_seatbelt_profile, probe_seatbelt
from .container import (
    CONTAINER_WORKSPACE,
    ContainerExecutionBackend,
    build_container_command,
    probe_container_runtime,
)
from .protocol import (
    EXECUTION_PROTOCOL_VERSION,
    CheckpointRef,
    CheckpointRequest,
    ExecutionEnvironment,
    ExecutionPolicy,
    FileMutationOperation,
    FileMutationRequest,
    FileReadResult,
    NetworkMode,
    ProcessRequest,
    ProcessResult,
    ReadFileRequest,
    RestoreRequest,
    RestoreResult,
)

__all__ = [
    "EXECUTION_PROTOCOL_VERSION",
    "CheckpointRef",
    "CheckpointRequest",
    "ExecutionEnvironment",
    "ExecutionPolicy",
    "FakeExecutionEnvironment",
    "FileMutationOperation",
    "LocalExecutionBackend",
    "CONTAINER_WORKSPACE",
    "ContainerExecutionBackend",
    "SeatbeltBackend",
    "build_container_command",
    "probe_container_runtime",
    "build_seatbelt_profile",
    "probe_seatbelt",
    "FileMutationRequest",
    "FileReadResult",
    "NetworkMode",
    "ProcessRequest",
    "ProcessResult",
    "ReadFileRequest",
    "RestoreRequest",
    "RestoreResult",
    "assert_execution_environment_conformance",
]