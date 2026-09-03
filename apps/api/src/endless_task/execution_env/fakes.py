from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from endless_task.agent_platform import EffectReceipt

from .protocol import (
    CheckpointRef,
    CheckpointRequest,
    ExecutionEnvironment,
    FileMutationRequest,
    FileReadResult,
    ProcessRequest,
    ProcessResult,
    ReadFileRequest,
    RestoreRequest,
    RestoreResult,
)


@dataclass
class FakeExecutionEnvironment(ExecutionEnvironment):
    file_read_result: Optional[FileReadResult] = None
    mutation_receipt: Optional[EffectReceipt] = None
    process_result: Optional[ProcessResult] = None
    checkpoint_ref: Optional[CheckpointRef] = None
    restore_result: Optional[RestoreResult] = None
    reads: list[ReadFileRequest] = field(default_factory=list)
    mutations: list[FileMutationRequest] = field(default_factory=list)
    processes: list[ProcessRequest] = field(default_factory=list)
    checkpoints: list[CheckpointRequest] = field(default_factory=list)
    restores: list[RestoreRequest] = field(default_factory=list)

    async def read_file(self, request: ReadFileRequest) -> FileReadResult:
        self.reads.append(request)
        if self.file_read_result is None:
            raise RuntimeError("Fake file read result is not configured")
        return self.file_read_result

    async def mutate_file(self, request: FileMutationRequest) -> EffectReceipt:
        self.mutations.append(request)
        if self.mutation_receipt is None:
            raise RuntimeError("Fake mutation receipt is not configured")
        return self.mutation_receipt

    async def run_process(self, request: ProcessRequest) -> ProcessResult:
        self.processes.append(request)
        if self.process_result is None:
            raise RuntimeError("Fake process result is not configured")
        return self.process_result

    async def checkpoint(self, request: CheckpointRequest) -> CheckpointRef:
        self.checkpoints.append(request)
        if self.checkpoint_ref is None:
            raise RuntimeError("Fake checkpoint reference is not configured")
        return self.checkpoint_ref

    async def restore(self, request: RestoreRequest) -> RestoreResult:
        self.restores.append(request)
        if self.restore_result is None:
            raise RuntimeError("Fake restore result is not configured")
        return self.restore_result