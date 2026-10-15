"""@ 引用 entry 的**写入 / 补偿 / 幂等预检**（非冻结薄包装）。

写入与补偿的约束：

1. 注入形态 = 一条 ``type=SYSTEM_NOTICE`` 的 ``v2_transcript_entries`` 行
   （``01`` §5.2 ③ 的首选形态：同时进压缩范围与优先级规划器）。
2. **注入必须发生在提交之前**：``execution_impl/runner.py:385`` 取上下文用的是
   ``list_entry_context_entries(run.trigger_entry_id)``（触发器 entry 的**祖先链**）。
   先 ``append_entry``（它会推进 lane leaf），提交路径再把用户 entry 挂到它下面
   ⇒ 引用 entry 自动成为触发器 entry 的祖先。提交之后再 append 是**静默失效**。
3. ``Database.transaction()`` **不可嵌套** ⇒ "append 引用 entry + 提交"是**两个事务**。
   ``create_message_submission`` / ``RuntimeV2SessionGateway`` 的签名不许改（也不必要），
   所以这里显式处理**孤儿 entry**：预检（把可预见失败挡在写入前）+ 失败补偿
   （把刚写的引用 entry **灭活**并把 lane leaf 还原）。

   补偿为什么是"灭活"而不是"删除"（复审 C-1）：``v2_transcript_entries.parent_id``
   有自引用 FK（``storage/migrations/036_runtime_v2.sql:41``），只要有一条别的 entry
   挂在我们写的 notice 下面（并发写入者），裸 ``DELETE`` 就会被 SQLite 拒绝并从
   ``transaction()`` 冒出 ``IntegrityError`` ⇒ 整个事务回滚（**一条都没删掉**）+ 异常
   上抛顶替原本的 409/202。灭活只改 ``context_policy_json``，不碰行与链，结构上
   不可能触发 FK，而且"孤儿即使留下也绝不再进任何一轮上下文"是可断言的事实
   （见 :func:`deactivate_reference_notices`）。

本模块是 ``mentions`` 包里**唯一**直接写库的地方；不新增任何仓储方法
（``storage_method_inventory.json`` 因此零改动）。
"""

from __future__ import annotations

import json
from typing import Optional, Sequence

from endless_task.runtime_v2.domain import (
    Actor,
    TranscriptEntryRecord,
    TranscriptEntryType,
)

#: 注入 entry 的类型/actor：``SYSTEM_NOTICE`` 默认策略
#: ``(include_in_llm=True, "full", TRUSTED)``（``runtime_v2/projection.py:67-68``），
#: 会投影成 ``role="system"``（``:191-192``）。
REFERENCE_NOTICE_TYPE = TranscriptEntryType.SYSTEM_NOTICE
REFERENCE_NOTICE_ACTOR = Actor.SYSTEM


def reference_context_policy(provenance: dict[str, object]) -> dict[str, object]:
    """注入 entry 的 ``context_policy``。

    前三个是既定键（``ContextPolicy.from_mapping`` 只 ``get`` 这三个），
    ``reference_provenance`` 是**自由键**——未知键被忽略，而记录上保留原始 JSON
    （``projection.py:34-52`` + ``storage/v2/base.py`` 的 ``_load``）
    ⇒ provenance 零 schema 变更、可审计。
    """

    return {
        "include_in_llm": True,
        "transform": "full",
        "trust_level": "trusted",
        "reference_provenance": dict(provenance),
    }


def append_reference_notice(
    repository,
    *,
    conversation_id: str,
    lane_id: str,
    text: str,
    provenance: dict[str, object],
) -> TranscriptEntryRecord:
    """写一条引用 notice。

    ``parent_id`` 不传 ⇒ ``append_entry`` 取**当前 lane leaf** 并把它推进到自己
    （``storage/v2/transcript.py:113-117``），因此连续多条的引用 entry 会自动串成链。
    """

    return repository.append_entry(
        conversation_id=conversation_id,
        lane_id=lane_id,
        type=REFERENCE_NOTICE_TYPE,
        actor=REFERENCE_NOTICE_ACTOR,
        payload={"content": text, "references": [dict(provenance)]},
        context_policy=reference_context_policy(provenance),
    )


def message_request_exists(
    database,
    *,
    conversation_id: str,
    client_request_id: str,
) -> bool:
    """``(conversation, Idempotency-Key)`` 是否已经提交过——幂等预检。

    为什么必须预检：重放时 ``create_message_submission`` 返回既有 run、**不新建**
    用户 entry，而我们先写的那条引用 entry 就成了**孤儿**（``include_in_llm=True``）
    ⇒ 之后每一轮都把被引用会话的正文投影进模型上下文，而用户从未发出那条消息。
    没有公开读面（``v2_message_requests`` 只有写入点），所以这里用一次只读查询，
    口径与提交闸的 SELECT 相同。
    """

    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT 1 FROM v2_message_requests
            WHERE conversation_id = ? AND client_request_id = ?
            LIMIT 1
            """,
            (conversation_id, client_request_id.strip()),
        ).fetchone()
    return row is not None


#: 补偿标记：写进 ``context_policy`` 的自由 JSON（未知键被 ``ContextPolicy`` 忽略，
#: 原始 JSON 保留在记录上 ⇒ 可审计"这条 notice 为什么被灭活"）。
COMPENSATION_MARKER = "reference_compensated"


def compensated_context_policy(
    raw_policy_json: Optional[str], reason: str
) -> str:
    """把一条 notice 的策略改成"再也进不了模型上下文"（保留其余自由键）。

    **必须是显式 ``False``、不能删键**：两个读面的缺省都是"进上下文"
    （``runtime_v2/projection.py:46`` ``value.get("include_in_llm", default)``、
    ``runtime_v2/context_segments.py:102`` ``policy.get("include_in_llm", True)``），
    删键等于重新放行。``transform="none"`` 是第二道锁（两个读面都要求
    ``transform != "none"``），与 include_in_llm 同时置上，形成冗余。
    """

    try:
        policy = json.loads(raw_policy_json) if raw_policy_json else {}
    except (TypeError, ValueError):
        policy = {}
    if not isinstance(policy, dict):
        policy = {}
    policy["include_in_llm"] = False
    policy["transform"] = "none"
    policy[COMPENSATION_MARKER] = reason
    return json.dumps(policy, ensure_ascii=False)


def deactivate_reference_notices(
    database,
    *,
    conversation_id: str,
    lane_id: str,
    entry_ids: Sequence[str],
    restore_leaf_entry_id: Optional[str],
    reason: str = "compensated",
) -> int:
    """补偿事务：**灭活**刚写的引用 notice（不删行），必要时把 lane leaf 还原。

    为什么不是 ``DELETE``（复审 C-1 的 Critical）：``parent_id`` 的自引用 FK
    （``036_runtime_v2.sql:41``）会让"notice 下已经有了并发子节点"的形态**必然**
    抛 ``sqlite3.IntegrityError``。裸删的后果不是"删不掉"这么轻：异常从
    ``transaction()`` 冒出 ⇒ 事务回滚（**一条都没处理**）+ 上抛顶替原本要返回的
    409/202，而 notice 仍是 ``include_in_llm=True`` ⇒ 被引用会话的正文从此**每一轮**
    都进模型上下文。灭活只 ``UPDATE context_policy_json``：不碰行、不碰链，
    结构上碰不到 FK。

    安全性（三条都是刻意的）：
    - 只动 ``type=system_notice`` 且 ``id`` 命中我们刚写的那些（误伤不可能）。
    - 只有"**当前 leaf 仍是我们写的那一条**"时才还原 leaf。notice 下面已经挂了
      并发写入者的 entry 时，**不动链**（还原 leaf 会把别人的 entry 从链上剪掉）。
    - 整件事在**一个** ``transaction()`` 里完成（``BEGIN IMMEDIATE``）。
    """

    targets = tuple(dict.fromkeys(entry_ids))
    if not targets:
        return 0
    placeholders = ", ".join("?" for _ in targets)
    with database.transaction() as connection:
        lane_row = connection.execute(
            "SELECT leaf_entry_id FROM v2_lanes WHERE id = ? AND conversation_id = ?",
            (lane_id, conversation_id),
        ).fetchone()
        if lane_row is None:
            return 0
        rows = connection.execute(
            "SELECT id, context_policy_json FROM v2_transcript_entries "
            "WHERE conversation_id = ? AND lane_id = ? AND type = ? "
            f"AND id IN ({placeholders})",
            (
                conversation_id,
                lane_id,
                REFERENCE_NOTICE_TYPE.value,
                *targets,
            ),
        ).fetchall()
        deactivated = 0
        for row in rows:
            connection.execute(
                "UPDATE v2_transcript_entries SET context_policy_json = ? "
                "WHERE id = ?",
                (compensated_context_policy(row["context_policy_json"], reason), row["id"]),
            )
            deactivated += 1
        if deactivated and lane_row["leaf_entry_id"] in set(targets):
            connection.execute(
                "UPDATE v2_lanes SET leaf_entry_id = ? WHERE id = ?",
                (restore_leaf_entry_id, lane_id),
            )
    return deactivated


def notice_is_ancestor_of(
    repository,
    *,
    notice_entry_ids: Sequence[str],
    trigger_entry_id: str,
) -> bool:
    """后置核验：刚写的引用 notice 是否**真的**在触发器 entry 的祖先链上。

    用与 runner **同一个**读面（``list_entry_context_entries``），因此这是端到端的
    机制证明，而不是"entry 存在"的间接推断。
    """

    wanted = set(notice_entry_ids)
    if not wanted:
        return True
    chain = repository.list_entry_context_entries(trigger_entry_id)
    return wanted.issubset({entry.id for entry in chain})
