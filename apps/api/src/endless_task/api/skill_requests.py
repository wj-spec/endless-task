"""S1：用户消息里的 ``/技能名`` 解析（从 app.py 搬出，去掉巨石依赖）。

行为零改动：报文格式、通知语义与 usage 记账规则都与搬家前一致。
"""

from __future__ import annotations

from endless_task.runtime.provider import ProviderMessage
from endless_task.skills import INVOKE_OK, INVOKE_UNKNOWN, parse_skill_commands


def resolve_skill_requests(
    skill_service: "SkillService",
    workspace_resolver: "WorkspaceResolver",
    conversation_id: str,
    content: str,
    *,
    include_bodies: bool,
    usage_recorder=None,
) -> tuple[tuple[object, ...], list[dict[str, str]]]:
    """S1：解析用户消息里的 ``/技能名``。

    返回 (user-role 消息元组, 结果通知列表)。名称不存在于任何技能时静默忽略
    （普通文本里的 ``/tmp`` 不应报错），只有"存在但不能用"才给通知。
    """
    names = parse_skill_commands(content)
    if not names:
        return (), []
    binding = workspace_resolver.resolve_binding(conversation_id)
    root = binding.root if binding is not None else None
    workspace_id = binding.workspace_id if binding is not None else ""
    messages: list[object] = []
    notices: list[dict[str, str]] = []
    for name in names:
        skill, reason = skill_service.resolve_invocable(
            name, root, workspace_id=workspace_id
        )
        if skill is None:
            if reason == INVOKE_UNKNOWN:
                continue
            notices.append(
                {
                    "name": name,
                    "status": reason,
                    "message": {
                        "disabled": "该技能已禁用。",
                        "not_user_invocable": "该技能不允许用户调用。",
                        "invalid": "该技能清单有错误，无法加载。",
                    }.get(reason, "该技能不可用。"),
                }
            )
            continue
        body = skill_service.skill_body(skill) if include_bodies else ""
        if include_bodies and not body:
            notices.append(
                {
                    "name": name,
                    "status": "unreadable",
                    "message": "技能正文读取失败。",
                }
            )
            continue
        notices.append({"name": name, "status": INVOKE_OK, "message": ""})
        if include_bodies:
            from endless_task.runtime.provider import ProviderMessage

            if usage_recorder is not None:
                usage_recorder(skill, "invoked")
                usage_recorder(skill, "body_read")

            messages.append(
                ProviderMessage(
                    role="user",
                    content=(
                        f'<skill_request name="{name}">\n{body}\n</skill_request>\n'
                        "（该技能正文已随本消息加载，无需再调用 read_skill_file；"
                        "只有需要技能目录内的其他资源文件时才读取。）"
                    ),
                )
            )
    return tuple(messages), notices
