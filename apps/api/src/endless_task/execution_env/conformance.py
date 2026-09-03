from __future__ import annotations

from endless_task.agent_platform import AgentPlatformError, EffectReceipt

from .protocol import ExecutionEnvironment, FileMutationRequest, ReadFileRequest


async def assert_execution_environment_conformance(
    environment: ExecutionEnvironment,
    *,
    read_request: ReadFileRequest,
    mutation_request: FileMutationRequest,
) -> EffectReceipt:
    read_result = await environment.read_file(read_request)
    if read_result.path != read_request.path:
        raise AgentPlatformError(
            "execution_read_path_mismatch",
            "Execution backend changed the requested read path",
        )
    receipt = await environment.mutate_file(mutation_request)
    if receipt.effect_id != mutation_request.effect_id:
        raise AgentPlatformError(
            "execution_effect_mismatch",
            "Execution backend changed the requested effect identifier",
        )
    if receipt.tool_call_id != mutation_request.tool_call_id:
        raise AgentPlatformError(
            "execution_tool_call_mismatch",
            "Execution receipt changed the originating tool call identifier",
        )
    return receipt