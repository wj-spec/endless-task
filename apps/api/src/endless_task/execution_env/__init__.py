"""Replaceable execution and side-effect backend contracts."""

from .conformance import assert_execution_environment_conformance
from .fakes import FakeExecutionEnvironment
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