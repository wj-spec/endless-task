"""应用设置（从 app.py 搬出；默认值、环境变量名与解析规则零改动）。

`AppSettings` 及其环境解析 helper 是纯配置层，不依赖容器，因此可以独立成模块。
`api/app.py` 仍 re-export `AppSettings`，既有 `from endless_task.api.app import AppSettings`
导入路径继续可用。
"""

from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from endless_task.delegation.scheduler import (
    DEFAULT_MAX_CHILDREN_PER_PARENT,
    DEFAULT_MAX_QUEUED_CHILDREN,
)
from endless_task.runtime.openai_compatible_provider import (
    DEFAULT_PROVIDER_MAX_RETRIES,
)
from endless_task.runtime_v2.run_budget import (
    CHILD_DEFAULT_COST_CAP_USD,
    CHILD_DEFAULT_MAX_HARD_SECONDS,
    DEFAULT_BUDGETS,
    RunBudget,
)
from endless_task.runtime_v2.verification import VERIFIER_MODES
from endless_task.workspace_runtime.shell_runner import (
    MIN_NO_CHANGE_TIMEOUT_SECONDS,
)


#: 单轮兜底在 provider 最坏合法耗时之外多留的余量（秒）。见
#: AppSettings._per_turn_backstop_seconds 的说明（R13）。
PER_TURN_BACKSTOP_MARGIN_SECONDS = 30.0

#: 可选的交互 run 档位（= `resolve_run_budget` 认识的运行类别）。
#: 单一来源是 `runtime_v2/run_budget.DEFAULT_BUDGETS` 的键，这里不重复字面量。
RUN_BUDGET_PROFILES: tuple[str, ...] = tuple(DEFAULT_BUDGETS)


#: 默认系统提示词。A7：引导模型对"多项对比/指标/步骤"用带语言标记的围栏块输出，
#: 前端会渲染成图表/指标卡/步骤清单（无法结构化时照常写 Markdown）。
DEFAULT_SYSTEM_PROMPT = (
    "你是 Endless Task，一个可靠、简洁的个人助手。\n"
    "- 能直接回答的问题用自然语言直接回答，不要调用工具。\n"
    "- 信息不足时先向用户追问关键信息，不要猜测。\n"
    "- 只有问题确实需要会话附件内容时才调用文件工具。\n"
    "- 文件操作优先用类型化工具：改已有文件里的一处用 edit_workspace_file（默认要求"
    "唯一匹配，改完会返回 diff）；移动/重命名/复制/建目录用 manage_workspace_paths；"
    "整文件新建或覆盖用 write_workspace_file；只有需要跑程序、构建、测试、装依赖或"
    "文本变换时才用 run_shell。\n"
    "- 工具执行失败或用户未授权时，用自然语言说明情况和下一步，不要原样重复同一调用。\n"
    "- 用户要求产出文档/文件（如写 README、报告、脚本）时，读完所需材料后应立即调用"
    "写入工具或直接给出完整结果，不要无休止地继续收集资料；读完即动手。\n"
    "- 当回答包含多项数值对比、指标汇总或步骤清单时，用带语言标记的围栏代码块给出"
    "结构化结果（前端会渲染成图表/指标卡/步骤清单），JSON 必须合法：\n"
    "  chart: {\"title\": \"\", \"unit\": \"\", \"series\": [{\"label\": \"\", \"value\": 0}]}\n"
    "  metrics: {\"title\": \"\", \"items\": [{\"label\": \"\", \"value\": \"\", \"hint\": \"\"}]}\n"
    "  steps: {\"title\": \"\", \"steps\": [{\"title\": \"\", \"detail\": \"\", \"done\": false}]}\n"
    "  其余内容照常写 Markdown；结构化块只是补充，不要用它替代解释。"
)


CONFIG_VERSION = 1


def default_data_directory() -> Path:
    configured = os.environ.get("ENDLESS_TASK_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Endless Task"
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Endless Task"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    return (
        Path(xdg_data_home).expanduser() / "endless-task"
        if xdg_data_home
        else Path.home() / ".local" / "share" / "endless-task"
    )


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def runtime_environment() -> dict[str, str]:
    """Merge the local .env file under real environment variables."""
    configured = os.environ.get("ENDLESS_TASK_ENV_FILE")
    candidates = (
        [Path(configured).expanduser()] if configured else [Path.cwd() / ".env"]
    )
    merged: dict[str, str] = {}
    for candidate in candidates:
        merged.update(_load_env_file(candidate))
    merged.update(os.environ)
    return merged


@dataclass(frozen=True)
class AppSettings:
    database_path: Path
    config_version: int = CONFIG_VERSION
    # v2 是唯一 runtime（14-runtime-single-mode）：旧 v1 引擎已删，无运行时切换。
    provider_name: str = "fake"
    model: str = "fake-model"
    base_url: Optional[str] = None
    api_key: Optional[str] = field(default=None, repr=False)
    provider_timeout_seconds: float = 60.0
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    system_prompt_version: str = "p1-v2"
    # 生产默认：128K 窗口 / 4K 输出。DeepSeek V4 系列实测窗口 1,048,576 tokens，
    # 但个人助手用 128K 已可容纳 40+ 轮工具调用；长任务可通过环境变量上调
    # （推荐 262144；极限 524288 需配合成本上限与上下文缓存）。
    context_window_tokens: int = 131_072
    max_output_tokens: int = 4_096
    summary_token_limit: int = 1_024
    max_concurrent_model_calls: int = 2
    max_concurrent_task_runs: int = 1
    max_tool_calls_per_turn: int = 8
    max_concurrent_tool_calls: int = 4
    max_tool_argument_bytes: int = 64 * 1024
    max_total_tool_argument_bytes: int = 256 * 1024
    max_tool_argument_depth: int = 32
    max_tool_argument_nodes: int = 10_000
    #: 旧配置字段：**只作为交互式档的覆盖**。None = 用户没设过 → 用新默认。
    #: 这样避免用"值等于 120.0 就当作没设"这种魔数启发式（显式设成 120 会被误判）。
    agent_timeout_seconds: Optional[float] = None
    approval_timeout_seconds: float = 1800.0
    max_message_characters: int = 100_000
    max_file_bytes: int = 1_000_000
    max_files_per_conversation: int = 10
    heartbeat_seconds: float = 15.0
    memory_proposals_enabled: bool = True
    memory_marker_gate_enabled: bool = True
    memory_auto_fact: bool = False
    context_compaction_enabled: bool = True
    # Agent Platform v2 tool path; default on after AP-107 flip. Illegal env values fail startup.
    tool_platform_v2_enabled: bool = True
    # M2 context engine v2 cutover; default on after validation (initial
    # context pruning under budget pressure; under-budget behavior is
    # byte-identical to legacy).
    context_engine_v2_enabled: bool = True
    # M4A read-only delegation mode; default "readonly" after real-provider
    # validation (2026-09-05 deepseek E2E: child run + concurrent batch +
    # delegate gate verified, 06 §27-29). "0" disables delegation tools.
    # "isolated_write" (M4B) is not implemented yet and fails startup
    # rather than silently degrading to read-only (no silent downgrade).
    delegation_mode: str = "readonly"
    # 切片 9 M2：子运行终态"回流"父会话的唤醒链路开关。默认开——唤醒是结果回流的
    # 可靠通道（注入块只保证"下一轮顺手可见"）。关掉只是不再自动起轮：结果仍会
    # 在下一轮运行的 <delegated-children> 注入里露面。
    child_wakeup_enabled: bool = True
    #: 合并窗口（秒）：同一会话窗口内到达的终态合成一条消息，避免子运行成群结束时
    #: 把父会话打成一堆并发轮次。
    child_wakeup_merge_window_seconds: float = 2.0
    #: 同时在飞的唤醒窗口上限（按会话计）；满员时新终态排队等收尾，不会丢。
    child_wakeup_max_inflight_conversations: int = 1
    #: M3-9 每会话投递节流（秒）：距本会话上次**成功投递**不足这么久时，下一个
    #: 唤醒窗口睡到边界再投——**只推迟、不丢批**。默认 60 秒：唤醒会**自动起一轮**
    #: （真花钱），而"子运行跑完 → 唤醒 → 父会话又派子运行 → 再唤醒"是一条能自己
    #: 转起来的链；节流给它一道速率上界。
    #:
    #: **0 是合法值**（语义 = 关掉节流）：与服务层默认 0.0 一致。而负数 / nan / inf
    #: 启动即失败并指名本变量：inf 让节流变成"永远不投"，负数与 nan 没有意义。
    child_wakeup_throttle_seconds: float = 60.0
    # M3A RS-1 (G1-2): provider retry mode (05 §RS-1). "0" (default) =
    # shadow only: evaluator observes and records retry decisions, never
    # retries (max_attempts=0); "1" enables bounded auto-retry of
    # pre-emission retryable provider errors. Illegal env values fail
    # startup.
    provider_retry_mode: str = "0"
    # M5 SK-1b: enable skill:// locator resolution for read_skill_file and
    # locator-based prompt exposure. Default off keeps the legacy absolute
    # path behavior unchanged. Illegal env values fail startup.
    skill_packages_enabled: bool = False
    # S5：默认技能目录预算（展开条目数 / 总字符）。见 05 文档 §3.2。
    skill_catalog_limit: int = 8
    skill_catalog_budget: int = 3_000
    # S8：生态安装权限（ask=逐次审批 / allow=用户已全局授权 / deny=禁止）。
    skill_install_mode: str = "ask"
    # M6 W6-2/W6-8: runtime trace mode (08 §OE-1). Default "all" after
    # real-provider validation (2026-09-05 deepseek E2E: usage rows +
    # run/model-turn spans + trajectory export all verified). "0" disables
    # all trace wiring; errors|sampled|all enable it. Illegal env values
    # fail startup.
    runtime_trace_mode: str = "all"
    # M6 W6-4: OTLP export mode (08 §OE-5). "0" (default) = exporter not
    # assembled; otlp-http exports terminal-run journal events to the
    # endpoint below. Illegal env values fail startup.
    otel_export_mode: str = "0"
    otel_export_endpoint: str = "http://127.0.0.1:4318/v1/traces"
    artifact_proposals_enabled: bool = True
    task_proposals_enabled: bool = True
    knowledge_proposals_enabled: bool = True
    knowledge_query_rewrite_enabled: bool = False
    knowledge_rerank_enabled: bool = False
    knowledge_scope_weights: dict = field(default_factory=dict)
    knowledge_synonym_map: dict = field(default_factory=dict)
    knowledge_duplicate_threshold: float = 0.5
    knowledge_decay_enabled: bool = True
    knowledge_decay_min_age_days: int = 30
    knowledge_decay_recheck_days: int = 90
    knowledge_decay_interval_hours: float = 24.0
    knowledge_feedback_enabled: bool = True
    embedding_enabled: bool = False
    embedding_backend: str = "local"
    embedding_model: Optional[str] = None
    embedding_local_repo: str = "Xenova/bge-small-zh-v1.5"
    embedding_local_url_base: str = "https://huggingface.co"
    embedding_max_chars: int = 1500
    embedding_batch_size: int = 8
    embedding_sqlite_vec: bool = False
    hybrid_literal_weight: float = 0.4
    # B1 近期性偏置：0 = 纯相关性（默认，行为不变）；τ 单位天。
    memory_recency_weight: float = 0.0
    memory_decay_tau_days: float = 30.0
    # B3 重要性加权遗忘：默认关闭（不自动遗忘用户记忆），打开后按间隔巡检。
    memory_forgetting_enabled: bool = False
    memory_forgetting_interval_hours: float = 24.0
    memory_forgetting_max_per_run: int = 10
    # B4 反思：终态运行（含失败）自动归纳洞见（走提案确认，默认开）。
    reflection_enabled: bool = True
    # B2 记忆巩固：相似度阈值与每次最多产出的巩固提案数。
    memory_consolidation_threshold: float = 0.5
    memory_consolidation_max_per_run: int = 3
    hybrid_semantic_weight: float = 0.6
    proposal_daily_budget: int = 6
    proposal_cooldown_minutes: int = 30
    proposal_quiet_start: str = "23:00"
    proposal_quiet_end: str = "07:00"
    scheduler_enabled: bool = True
    scheduler_tick_seconds: float = 30.0
    task_run_review_enabled: bool = True
    notifications_enabled: bool = True
    #: C3a / `02 §3.1` 决策 3：@ 引用的"**整段**"档（``mode="full"``）**默认禁用**，
    #: 需显式开启（`ENDLESS_TASK_MENTION_FULL_MODE=1`）。关闭时带 ``mode="full"``
    #: 的请求被**指名 400** 拒绝——不做静默降级（降级会让用户以为整段被带过去了）。
    mention_full_mode_enabled: bool = False
    task_max_attempts: int = 3
    task_retry_backoff_seconds: float = 60.0
    #: `run_shell` 的**总时长墙**（秒）。默认 120 = 历史行为；装配时钳到
    #: `shell_runner` 的 [1, 600] 区间，且**内外层同源**（内层 runner 与外层工具
    #: 定义的 `timeout_seconds` 都用它），否则就会出现"外层 600、内层 120"。
    shell_timeout_seconds: float = 120.0
    #: `run_shell` 的**无输出看门狗**（秒）：多久没有任何 stdout/stderr 就认为卡死。
    #: 与 `shell_timeout_seconds`（总时长墙）是**两件事**：这条是"安静多久算死"，
    #: 那条是"最长允许跑多久"。语义：0 = 关闭看门狗；(0, 1.0) 钳到 1.0 秒（防
    #: "一有输出延迟就杀"）；负数/非有限值装配期**指名报错**。默认 60 逐字不变。
    shell_no_change_timeout_seconds: float = 60.0
    shell_max_output_bytes: int = 65_536
    workspace_max_write_bytes: int = 512_000
    # M3B slice B (方案 A 兑现): a v2 run that reaches terminal FAILED
    # auto-restores its workspace checkpoints (guards keep user edits).
    # Default on after run-level checkpoint landed (slice A) + guarded
    # restore E2E; "0" disables so ops keep partial state on failure.
    run_auto_restore_enabled: bool = True
    # M3B slice F/F2 enforcement: execution backend for unattended executors'
    # workspace fs mutations. "local" (default since F2) routes unattended
    # write/delete through the ExecutionEnvironment seam (containment
    # re-check + run-scoped ledger + single execution boundary); "container"
    # is the same seam for file mutations (container's isolation difference
    # is process-bound, which unattended cannot reach today); "" disables
    # enforcement (current direct tool execution). Illegal env values fail
    # startup.
    execution_backend_mode: str = "local"  # values: "" | local | container
    # G1 item 5: no-progress StopPolicy enforcement (05 §RS-2). Default off;
    # thresholds approved (remind=1/restrict=2/stop=3 consecutive signals).
    # "1" turns on the safety stop for runs that make no progress.
    stop_policy_enforcement: bool = False
    # C4: 上下文预算用掉多少比例就升级人工（无进展升级与阈值无关）。
    escalation_budget_ratio: float = 0.85
    # C1: 独立验证模式 —— 0 关（默认）/ 1 所有运行 / side_effects 仅有副作用的
    # 关键运行；verifier_model 为空时用主模型（上下文与 prompt 仍完全独立）。
    verifier_mode: str = "0"
    verifier_model: Optional[str] = None
    #: 旧配置字段：**只作为交互式档成本上限的覆盖**。None = 用户没设过 → 用新默认。
    #: （同 agent_timeout_seconds 的哨兵语义；显式设成 0 仍表示"关闭成本断路器"。）
    cost_cap_usd: Optional[float] = None
    #: 三类运行预算。时长/成本"按类别"而不是一个全局墙：交互式 15 分钟够用，
    #: 定时任务需要一小时，显式长跑另算。
    interactive_soft_seconds: float = 180.0
    interactive_hard_seconds: float = 900.0
    interactive_cost_cap_usd: float = 1.0
    task_soft_seconds: float = 600.0
    task_hard_seconds: float = 3600.0
    task_cost_cap_usd: float = 3.0
    long_soft_seconds: float = 3600.0
    long_hard_seconds: float = 14400.0
    long_cost_cap_usd: float = 10.0
    #: **交互 run 用哪一档预算**（切片 10 T1 Step 4）。取值就是 `resolve_run_budget`
    #: 认识的三个运行类别：``interactive``（默认，= 今天的行为）/ ``task`` / ``long``。
    #:
    #: 为什么需要它：``long`` 档（soft 3600 / hard 14400 / $10）此前**没有任何调用方**
    #: ——`ENDLESS_TASK_LONG_*` 改的是没人读的值，文档承诺的"仅 env 可启用"是假的
    #: （`run_budget.py` 的 `LONG_BUDGET` 是死配置）。本设置把这条线接到交互网关上，
    #: 让 `ENDLESS_TASK_LONG_*` 真正生效。
    #:
    #: **作用域只有交互 run**：定时/任务 run 继续 `resolve_run_budget("task")`，
    #: 委派子运行继续用 `child_run_budget`（CE-5「Task/Unattended 使用独立 budget
    #: profile」是硬契约，本设置不得把它们并回交互档）。变量名按既有命名纪律取
    #: `ENDLESS_TASK_RUN_BUDGET_PROFILE`（与 `..._DELEGATION`/`..._EXECUTION_BACKEND`
    #: 同形：一个选择器 + 明确取值域）；非法值装配期**指名报错**，不静默回落。
    run_budget_profile: str = "interactive"
    #: 子运行的硬墙**上限**（秒，M3-1）：派生预算时钳制模型声明的
    #: `SpawnSpec.timeout_seconds`——它收得到 86400（一天），不设上限时一个子运行
    #: 能占着并发名额与在册配额一整天。默认与任务档 hard 同量级（3600，常量在
    #: `runtime_v2/run_budget.CHILD_DEFAULT_MAX_HARD_SECONDS`，不在这里重复字面量）。
    #:
    #: **行为后果**：模型声明超过本上限的墙会被钳到本值（这正是本设置的用途），
    #: 声明更小的值不受影响；下限（`CHILD_MIN_HARD_SECONDS = 1.0`）优先于本上限——
    #: 即便把它配成小于 1 的数，子运行仍拿到一次收尾机会。派生预算的另一条路径
    #: （`deadline_monotonic is None`，即任务档回退）与本上限无关。
    #:
    #: 非法值（非数字 / 0 / 负数 / nan / inf）启动即失败并指名本变量：inf 让上限
    #: 名存实亡，0 把每个子运行压到 1 秒——两者都不是"关掉上限"的表达方式。
    max_child_hard_seconds: float = CHILD_DEFAULT_MAX_HARD_SECONDS
    #: 子运行的**成本上限**（美元，M3-3）。默认与任务档同价（3.0，常量在
    #: `runtime_v2/run_budget.CHILD_DEFAULT_COST_CAP_USD`，不在这里重复字面量），
    #: 所以子运行**默认就有**一道真成本上界：到顶走优雅收尾（COMPLETED + 引导语），
    #: 而不是"提请一次然后继续烧"。派生预算上的 `stop_on_cost=True` 就是这个策略。
    #:
    #: **0 是合法值**：语义是"关掉子运行成本上界"（cap=0 时 `cost_cap_exceeded`
    #: 恒 False）。与墙上限不同，把 cap 配成 0 不会把子运行压到地板，所以它是
    #: 表达"关闭"的正规方式；负数 / nan / inf 则启动即失败并指名本变量。
    #:
    #: **必须如实知道的前提**：这道闸要**模型可定价**才会触发。
    #: `estimate_cost_usd` 对未定价的 provider/model 返回 None，而
    #: `cost_cap_exceeded(cost_usd=None, ...)` 恒为 False——没有定价时"配置位存在
    #: 但拦不住"，子运行照旧跑完。别把本设置当成"一定有成本上界"的保证。
    max_child_cost_usd: float = CHILD_DEFAULT_COST_CAP_USD
    #: 委派子运行的**默认墙**（秒，切片 10 T1 Step 3）：模型在 `spawn_agent` 里
    #: 不写 `timeoutSeconds` 时，子运行能活多久。默认 120.0 = 今天写死在
    #: `CoordinatorDelegationHandler.__init__` 默认参数里的那个值（逐字不变）。
    #:
    #: 为什么要有它：那个 120 秒是**第二处 120 秒墙**，与 `run_shell` 的那处
    #: 并列（`wiring.py` 构造 handler 时没传，装配根无从调整）。子运行转后台之后
    #: "模型忘了声明超时"就等于"子运行只活两分钟"，与 run 预算（交互 900s /
    #: 任务 3600s）比小一个量级。
    #:
    #: 仍然被 `max_child_hard_seconds`（默认 3600）**封顶**：本值调大只把
    #: "默认声明"抬高，钳制发生在派生预算那一步（`child_run_budget`），所以调成
    #: 7200 也只能拿到 3600；调小则立即生效。非法值（非数字 / 0 / 负数 /
    #: nan / inf）启动即失败并指名本变量。
    child_default_timeout_seconds: float = 120.0
    #: 在册（已预留、未终态）子运行的**绝对**上限（M3 Task 7）。在册容量
    #: （`max_children_per_parent`，默认 8）从"拒绝"改成"排队"之后，超出的子运行
    #: 在 admission 闸门外排队；本上限是那道防无界队列的墙，到顶仍拒绝并指名报错。
    #:
    #: 默认 32 = 在册容量的四倍（常量在 `delegation/scheduler.py`，不在这里重复
    #: 字面量）。非法值（非数字 / 0 / 负数）启动即失败并指名本变量："上限 0" 会让
    #: 所有子运行都派不出去，不是"关掉上限"的表达方式。
    max_queued_children: int = DEFAULT_MAX_QUEUED_CHILDREN

    def __post_init__(self) -> None:
        if self.config_version != CONFIG_VERSION:
            raise ValueError(
                f"Unsupported config version {self.config_version}; expected {CONFIG_VERSION}"
            )
        if self.provider_timeout_seconds <= 0 or self.heartbeat_seconds <= 0:
            raise ValueError("Timeout values must be positive")
        if self.context_window_tokens <= self.max_output_tokens:
            raise ValueError("Context window must be larger than max output tokens")
        if self.cost_cap_usd is not None and self.cost_cap_usd < 0:
            raise ValueError("ENDLESS_TASK_COST_CAP_USD 不能为负数")
        if self.verifier_mode not in VERIFIER_MODES:
            raise ValueError(
                "ENDLESS_TASK_VERIFIER 只允许 0 | 1 | side_effects"
            )
        if not 0 < self.escalation_budget_ratio <= 1:
            raise ValueError("Escalation budget ratio must be within (0, 1]")
        if self.summary_token_limit < 0:
            raise ValueError("Summary token limit cannot be negative")
        if self.max_concurrent_model_calls <= 0:
            raise ValueError("Maximum concurrent model calls must be positive")
        if self.max_queued_children <= 0:
            raise ValueError("ENDLESS_TASK_MAX_QUEUED_CHILDREN 必须是正整数")
        # 绝对上限不得低于在册容量（装配层只把本设置交给 ChildBatchLimits，
        # 另外两个容量位保持默认）：低于时"排队"这条闸门自相矛盾。这条在
        # ChildBatchLimits.__post_init__ 里仍然是不变量，但那条消息只说法则、
        # 不点环境变量名，运维拿到 `invalid_child_batch_limits` 只能靠猜；
        # 所以这里先拦住并**指名设置 + 下限**。
        if self.max_queued_children < DEFAULT_MAX_CHILDREN_PER_PARENT:
            raise ValueError(
                "ENDLESS_TASK_MAX_QUEUED_CHILDREN 不能小于在册容量"
                f"（下限 {DEFAULT_MAX_CHILDREN_PER_PARENT}，"
                "即 max_children_per_parent 的默认值）"
            )
        tool_limit_values = (
            self.max_tool_calls_per_turn,
            self.max_concurrent_tool_calls,
            self.max_tool_argument_bytes,
            self.max_total_tool_argument_bytes,
            self.max_tool_argument_depth,
            self.max_tool_argument_nodes,
        )
        if any(value <= 0 for value in tool_limit_values):
            raise ValueError("Tool limits must be positive")
        if self.max_tool_argument_bytes > self.max_total_tool_argument_bytes:
            raise ValueError(
                "Per-call tool argument limit cannot exceed the per-turn limit"
            )
        if self.agent_timeout_seconds is not None and self.agent_timeout_seconds <= 0:
            raise ValueError("Agent timeout must be positive")
        if self.approval_timeout_seconds <= 0:
            raise ValueError("Approval timeout must be positive")
        if self.scheduler_tick_seconds <= 0:
            raise ValueError("Scheduler tick must be positive")
        if self.embedding_max_chars <= 0 or self.embedding_batch_size <= 0:
            raise ValueError("Embedding limits must be positive")
        if not 0 <= self.memory_recency_weight <= 1:
            raise ValueError("ENDLESS_TASK_MEMORY_RECENCY_WEIGHT must be within [0, 1]")
        if self.memory_decay_tau_days <= 0:
            raise ValueError("ENDLESS_TASK_MEMORY_DECAY_TAU must be positive")
        if self.memory_forgetting_interval_hours <= 0:
            raise ValueError("ENDLESS_TASK_MEMORY_FORGETTING_INTERVAL_HOURS must be positive")
        if not 0 < self.memory_consolidation_threshold <= 1:
            raise ValueError("ENDLESS_TASK_MEMORY_CONSOLIDATION_THRESHOLD must be within (0, 1]")
        if self.memory_consolidation_max_per_run < 1:
            raise ValueError("ENDLESS_TASK_MEMORY_CONSOLIDATION_MAX_PER_RUN must be >= 1")
        if self.memory_forgetting_max_per_run < 1:
            raise ValueError("ENDLESS_TASK_MEMORY_FORGETTING_MAX_PER_RUN must be >= 1")
        if self.hybrid_literal_weight < 0 or self.hybrid_semantic_weight < 0:
            raise ValueError("Hybrid weights cannot be negative")
        if not 0 < self.knowledge_duplicate_threshold <= 1:
            raise ValueError("Knowledge duplicate threshold must be in (0, 1]")
        if self.knowledge_decay_min_age_days < 1 or self.knowledge_decay_recheck_days < 1:
            raise ValueError("Knowledge decay windows must be positive")
        if self.knowledge_decay_interval_hours <= 0:
            raise ValueError("Knowledge decay interval must be positive")
        if self.task_max_attempts < 1:
            raise ValueError("Task max attempts must be positive")
        if self.task_retry_backoff_seconds <= 0:
            raise ValueError("Task retry backoff must be positive")
        if self.max_message_characters <= 0:
            raise ValueError("Maximum message characters must be positive")
        if self.max_file_bytes <= 0 or self.max_files_per_conversation <= 0:
            raise ValueError("File limits must be positive")
        # M3-1：与三档时长同一把尺子（正数 + 有限）。env 路径由带名解析器兜住，
        # 这里兜的是代码里手写构造 AppSettings 的路径。
        if (
            not math.isfinite(self.max_child_hard_seconds)
            or self.max_child_hard_seconds <= 0
        ):
            raise ValueError("ENDLESS_TASK_MAX_CHILD_HARD_SECONDS 必须是正数")
        # M3-3：子运行成本上限。这里兜的是代码里手写构造 AppSettings 的路径
        # （env 路径由 `_parse_non_negative_float` 带名兜住）。0 合法 = 关闭上界；
        # 负数 / nan / inf 都不是"关闭"的表达方式（inf 让 cap 变成没上限的空话）。
        if (
            not math.isfinite(self.max_child_cost_usd)
            or self.max_child_cost_usd < 0
        ):
            raise ValueError("ENDLESS_TASK_MAX_CHILD_COST_USD 必须是非负数字")
        # 切片 10 T1 Step 3：子运行默认墙。env 路径由 `_parse_positive_float` 兜住，
        # 这里兜"代码里手写构造 AppSettings"。0/负数会把每个不写 timeoutSeconds 的
        # 子运行压成"立刻收尾"，inf 让默认墙名存实亡——都不是合法值。
        if (
            not math.isfinite(self.child_default_timeout_seconds)
            or self.child_default_timeout_seconds <= 0
        ):
            raise ValueError("ENDLESS_TASK_CHILD_DEFAULT_TIMEOUT_SECONDS 必须是正数")
        # 切片 10 T1 Step 4：交互 run 档位选择器。非法值**装配期大声失败**，
        # 不静默回落到 interactive（静默回落会让"我配了 long 却没生效"无从察觉）。
        if self.run_budget_profile not in RUN_BUDGET_PROFILES:
            raise ValueError(
                "ENDLESS_TASK_RUN_BUDGET_PROFILE 只允许 "
                + " | ".join(RUN_BUDGET_PROFILES)
            )
        # 切片 10 T1 Step 2：无输出看门狗。语义与 runner 的
        # `resolve_no_change_timeout_seconds` 对齐——0 = 关闭看门狗（合法显式
        # opt-out）；(0, MIN) 钳到 MIN（防"一有输出延迟就杀"）；
        # 负数 / nan / inf 不是"关闭"的表达方式，指名报错。
        if (
            not math.isfinite(self.shell_no_change_timeout_seconds)
            or self.shell_no_change_timeout_seconds < 0
        ):
            raise ValueError(
                "ENDLESS_TASK_SHELL_NO_CHANGE_TIMEOUT_SECONDS 必须是非负数字"
            )
        if self.shell_no_change_timeout_seconds > 0:
            object.__setattr__(
                self,
                "shell_no_change_timeout_seconds",
                max(
                    self.shell_no_change_timeout_seconds,
                    MIN_NO_CHANGE_TIMEOUT_SECONDS,
                ),
            )
        # M3-9：每会话唤醒节流。这里兜的是代码里手写构造 AppSettings 的路径（env
        # 路径由 `_parse_non_negative_float` 带名兜住）。0 合法 = 关掉节流（与服务层
        # 默认一致）；负数 / nan / inf 都不是"关掉"的表达方式。
        if (
            not math.isfinite(self.child_wakeup_throttle_seconds)
            or self.child_wakeup_throttle_seconds < 0
        ):
            raise ValueError(
                "ENDLESS_TASK_CHILD_WAKEUP_THROTTLE_SECONDS 必须是非负数字"
            )
        self._build_run_budgets()

    def _per_turn_backstop_seconds(self) -> float:
        """单次模型调用的兜底上限：**必须严格大于** provider 自己可能耗费的时间。

        为什么不能取等（Task 5 评审实测的后果）：provider 适配器为自己的超时抛
        `request_timeout`，它同时
          ① 在适配器内部是**可重试**的（`_PRE_EVENT_RETRYABLE_ERROR_CODES` 含它，
             最多再重试 `DEFAULT_PROVIDER_MAX_RETRIES` 次）；
          ② 在 `reliability/retry.py:DEFAULT_ERROR_CODE_MAP` 里映射到
             `PROVIDER_TIMEOUT` —— **可重试且 `failover_allowed=True`**。
        而单轮兜底抛的 `agent_timeout` **根本不在这张表里**，按该文件注释
        "unknown codes classify as non-retryable (fail closed)" **不可重试**。

        更糟的是这不是五五开的竞态而是**系统性**的：外层 `asyncio.timeout` 的时钟
        在 `run_next_model_turn()` **之前**就起算（里面还要跑 steering 排空与
        compaction），所以外层的到期时间**早于**适配器自己的 HTTP 到期时间，
        provider 的超时与它的重试都被我们抢先掐掉。`hard_seconds=None` 时更明显
        ——那条分支过去根本没有外层包装，重试是存在过的。

        因此兜底必须覆盖 provider 的**最坏合法耗时**：
        `provider_timeout_seconds × (重试次数 + 1) + 余量`
        （60 × 3 + 30 = 210 秒，仍远小于交互档 900 秒的墙，所以它照样先于墙兜住真挂死）。
        """
        return (
            self.provider_timeout_seconds * (DEFAULT_PROVIDER_MAX_RETRIES + 1)
            + PER_TURN_BACKSTOP_MARGIN_SECONDS
        )

    def _build_run_budgets(self) -> None:
        """把三档预算合成为 ``RunBudget``（冻结 dataclass 用 ``object.__setattr__``）。"""
        # 旧配置兼容：显式设过 ENDLESS_TASK_AGENT_TIMEOUT_SECONDS / COST_CAP_USD
        # 的老用户，含义原样保留——只覆盖交互式档，不动任务/长跑档。
        legacy_hard = self.agent_timeout_seconds
        interactive_hard = (
            legacy_hard if legacy_hard is not None else self.interactive_hard_seconds
        )
        interactive_cost = (
            self.cost_cap_usd
            if self.cost_cap_usd is not None
            else self.interactive_cost_cap_usd
        )
        interactive_soft = self.interactive_soft_seconds
        if legacy_hard is not None:
            # 夹取**只作用于"旧配置覆盖"这条路径**（R16）：老变量（默认 120 的老语义）
            # 可能把 hard 压到 soft 之下甚至相等，而 RunBudget 要求 soft 严格小于 hard；
            # 老配置没有 soft 这个概念，所以由我们替它兜底（相等时取一半）。
            #
            # 显式写进新 env 的 soft **不在这里被改写**：它要么被采纳，要么由下面的
            # 交叉校验指名报错。否则 `INTERACTIVE_SOFT=2000, HARD=900` 会被静默改成
            # 450，而任务/长跑档同样的错配却会 fail fast——同一份配置两套行为。
            interactive_soft = min(interactive_soft, interactive_hard)
            if interactive_soft >= interactive_hard:
                interactive_soft = interactive_hard / 2.0
        self._validate_tier_lengths(
            interactive_soft=interactive_soft,
            interactive_hard=interactive_hard,
        )
        per_turn = self._per_turn_backstop_seconds()
        object.__setattr__(
            self,
            "interactive_budget",
            RunBudget(
                soft_seconds=interactive_soft,
                hard_seconds=interactive_hard,
                cost_cap_usd=interactive_cost,
                per_turn_seconds=per_turn,
            ),
        )
        object.__setattr__(
            self,
            "task_budget",
            RunBudget(
                soft_seconds=self.task_soft_seconds,
                hard_seconds=self.task_hard_seconds,
                cost_cap_usd=self.task_cost_cap_usd,
                per_turn_seconds=per_turn,
            ),
        )
        object.__setattr__(
            self,
            "long_budget",
            RunBudget(
                soft_seconds=self.long_soft_seconds,
                hard_seconds=self.long_hard_seconds,
                cost_cap_usd=self.long_cost_cap_usd,
                per_turn_seconds=per_turn,
            ),
        )

    def _validate_tier_lengths(
        self, *, interactive_soft: float, interactive_hard: float
    ) -> None:
        """三档的 soft 必须严格小于 hard，且报错要**指名**是哪个环境变量。

        为什么不靠 RunBudget 自己的校验做这件事：它的消息是英文的
        ``soft_seconds must be smaller than hard_seconds``，不告诉用户该改哪一行
        配置；而 `HARD=0` 这类值还会在 `min()` 之后以
        ``soft_seconds must be positive or None`` 的形式**指错字段**。
        这里统一按"变量名对变量名"报错，三档一把尺子。
        """
        tiers = (
            (
                "ENDLESS_TASK_INTERACTIVE_SOFT_SECONDS",
                "ENDLESS_TASK_INTERACTIVE_HARD_SECONDS",
                interactive_soft,
                interactive_hard,
            ),
            (
                "ENDLESS_TASK_TASK_SOFT_SECONDS",
                "ENDLESS_TASK_TASK_HARD_SECONDS",
                self.task_soft_seconds,
                self.task_hard_seconds,
            ),
            (
                "ENDLESS_TASK_LONG_SOFT_SECONDS",
                "ENDLESS_TASK_LONG_HARD_SECONDS",
                self.long_soft_seconds,
                self.long_hard_seconds,
            ),
        )
        for soft_name, hard_name, soft, hard in tiers:
            if soft >= hard:
                raise ValueError(f"{soft_name} 必须小于 {hard_name}")

    def resolve_run_budget(self, run_class: str) -> "RunBudget":
        """按运行类别取预算（交互式 / 任务 / 长跑）；未知类别启动即失败。"""
        if run_class == "interactive":
            return self.interactive_budget
        if run_class == "task":
            return self.task_budget
        if run_class == "long":
            return self.long_budget
        raise ValueError(f"unknown run class: {run_class!r}")

    @classmethod
    def from_environment(cls) -> "AppSettings":
        env = runtime_environment()
        configured = env.get("ENDLESS_TASK_DB_PATH")
        provider_name = env.get("ENDLESS_TASK_PROVIDER", "fake").strip().lower()
        defaults = {
            "fake": ("fake-model", None),
            "deepseek": ("deepseek-chat", "https://api.deepseek.com"),
            "openai": ("gpt-4.1-mini", "https://api.openai.com/v1"),
            "openai-compatible": ("", None),
        }
        default_model, default_base_url = defaults.get(provider_name, ("", None))
        api_key = env.get("ENDLESS_TASK_API_KEY")
        if api_key is None and provider_name == "deepseek":
            api_key = env.get("DEEPSEEK_API_KEY")
        if api_key is None and provider_name == "openai":
            api_key = env.get("OPENAI_API_KEY")
        database_path = (
            Path(configured).expanduser().resolve()
            if configured
            else default_data_directory() / "endless-task.db"
        )
        return cls(
            database_path=database_path,
            memory_proposals_enabled=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_PROPOSALS", "1")
            ),
            memory_marker_gate_enabled=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_MARKER_GATE", "1")
            ),
            memory_auto_fact=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_AUTO_FACT", "0")
            ),
            context_compaction_enabled=_parse_flag(
                env.get("ENDLESS_TASK_CONTEXT_COMPACTION", "1")
            ),
            tool_platform_v2_enabled=_parse_strict_flag(
                env.get("ENDLESS_TASK_TOOL_PLATFORM_V2", "1"),
                name="ENDLESS_TASK_TOOL_PLATFORM_V2",
            ),
            context_engine_v2_enabled=_parse_strict_flag(
                env.get("ENDLESS_TASK_CONTEXT_ENGINE_V2", "1"),
                name="ENDLESS_TASK_CONTEXT_ENGINE_V2",
            ),
            delegation_mode=_parse_delegation_mode(
                env.get("ENDLESS_TASK_DELEGATION", "readonly"),
            ),
            child_wakeup_enabled=_parse_strict_flag(
                env.get("ENDLESS_TASK_CHILD_WAKEUP", "1"),
                name="ENDLESS_TASK_CHILD_WAKEUP",
            ),
            child_wakeup_merge_window_seconds=_parse_positive_float(
                env.get("ENDLESS_TASK_CHILD_WAKEUP_WINDOW_SECONDS", "2.0"),
                name="ENDLESS_TASK_CHILD_WAKEUP_WINDOW_SECONDS",
            ),
            child_wakeup_max_inflight_conversations=_parse_positive_int(
                env.get("ENDLESS_TASK_CHILD_WAKEUP_MAX_INFLIGHT", "1"),
                name="ENDLESS_TASK_CHILD_WAKEUP_MAX_INFLIGHT",
            ),
            # M3-9：每会话投递节流。复用 `_parse_non_negative_float`（0 合法 =
            # 关掉节流），而不是 `_parse_positive_float` 的非负变体——同一个语义
            # （"0 表示关闭这道闸"）在成本上限那里已经有一把带名解析器，不再造第二把。
            child_wakeup_throttle_seconds=_parse_non_negative_float(
                env.get("ENDLESS_TASK_CHILD_WAKEUP_THROTTLE_SECONDS", "60"),
                name="ENDLESS_TASK_CHILD_WAKEUP_THROTTLE_SECONDS",
            ),
            provider_retry_mode=_parse_strict_mode(
                env.get("ENDLESS_TASK_PROVIDER_RETRY_V2", "0"),
                name="ENDLESS_TASK_PROVIDER_RETRY_V2",
            ),
            skill_install_mode=_parse_skill_install_mode(
                env.get("ENDLESS_TASK_SKILL_INSTALL", "ask")
            ),
            skill_catalog_limit=int(
                os.environ.get("ENDLESS_TASK_SKILL_CATALOG_LIMIT") or 8
            ),
            skill_catalog_budget=int(
                os.environ.get("ENDLESS_TASK_SKILL_CATALOG_BUDGET") or 3_000
            ),
            skill_packages_enabled=_parse_strict_flag(
                env.get("ENDLESS_TASK_SKILL_PACKAGES", "0"),
                name="ENDLESS_TASK_SKILL_PACKAGES",
            ),
            # C3a：@ 引用的"整段"档默认禁用；非法值启动即失败（与既有开关同规）。
            mention_full_mode_enabled=_parse_strict_flag(
                env.get("ENDLESS_TASK_MENTION_FULL_MODE", "0"),
                name="ENDLESS_TASK_MENTION_FULL_MODE",
            ),
            runtime_trace_mode=_parse_runtime_trace_mode(
                env.get("ENDLESS_TASK_RUNTIME_TRACE", "all"),
            ),
            otel_export_mode=_parse_otel_export_mode(
                env.get("ENDLESS_TASK_OTEL_EXPORT", "0"),
            ),
            run_auto_restore_enabled=_parse_flag(
                env.get("ENDLESS_TASK_RUN_AUTO_RESTORE", "1")
            ),
            execution_backend_mode=_parse_execution_backend_mode(
                env.get("ENDLESS_TASK_EXECUTION_BACKEND", "local")
            ),
            stop_policy_enforcement=_parse_strict_flag(
                env.get("ENDLESS_TASK_STOP_POLICY_V2", "0"),
                name="ENDLESS_TASK_STOP_POLICY_V2",
            ),
            escalation_budget_ratio=_parse_ratio(
                env.get("ENDLESS_TASK_ESCALATION_BUDGET_RATIO", "0.85"),
                name="ENDLESS_TASK_ESCALATION_BUDGET_RATIO",
            ),
            verifier_mode=_parse_verifier_mode(
                env.get("ENDLESS_TASK_VERIFIER", "0"),
            ),
            verifier_model=(
                env.get("ENDLESS_TASK_VERIFIER_MODEL", "").strip() or None
            ),
            # 哨兵语义：老变量只在**显式存在**时写入（否则留 None → 用按类别的新默认）。
            cost_cap_usd=(
                _parse_non_negative_float(
                    env["ENDLESS_TASK_COST_CAP_USD"],
                    name="ENDLESS_TASK_COST_CAP_USD",
                )
                if "ENDLESS_TASK_COST_CAP_USD" in env
                else None
            ),
            interactive_soft_seconds=_parse_positive_float(
                env.get("ENDLESS_TASK_INTERACTIVE_SOFT_SECONDS", "180"),
                name="ENDLESS_TASK_INTERACTIVE_SOFT_SECONDS",
            ),
            interactive_hard_seconds=_parse_positive_float(
                env.get("ENDLESS_TASK_INTERACTIVE_HARD_SECONDS", "900"),
                name="ENDLESS_TASK_INTERACTIVE_HARD_SECONDS",
            ),
            interactive_cost_cap_usd=_parse_non_negative_float(
                env.get("ENDLESS_TASK_INTERACTIVE_COST_CAP_USD", "1"),
                name="ENDLESS_TASK_INTERACTIVE_COST_CAP_USD",
            ),
            task_soft_seconds=_parse_positive_float(
                env.get("ENDLESS_TASK_TASK_SOFT_SECONDS", "600"),
                name="ENDLESS_TASK_TASK_SOFT_SECONDS",
            ),
            task_hard_seconds=_parse_positive_float(
                env.get("ENDLESS_TASK_TASK_HARD_SECONDS", "3600"),
                name="ENDLESS_TASK_TASK_HARD_SECONDS",
            ),
            task_cost_cap_usd=_parse_non_negative_float(
                env.get("ENDLESS_TASK_TASK_COST_CAP_USD", "3"),
                name="ENDLESS_TASK_TASK_COST_CAP_USD",
            ),
            long_soft_seconds=_parse_positive_float(
                env.get("ENDLESS_TASK_LONG_SOFT_SECONDS", "3600"),
                name="ENDLESS_TASK_LONG_SOFT_SECONDS",
            ),
            long_hard_seconds=_parse_positive_float(
                env.get("ENDLESS_TASK_LONG_HARD_SECONDS", "14400"),
                name="ENDLESS_TASK_LONG_HARD_SECONDS",
            ),
            long_cost_cap_usd=_parse_non_negative_float(
                env.get("ENDLESS_TASK_LONG_COST_CAP_USD", "10"),
                name="ENDLESS_TASK_LONG_COST_CAP_USD",
            ),
            # M3-1：子运行墙上限。放在三档时长旁边，一眼能看出它跟预算是一族。
            max_child_hard_seconds=_parse_positive_float(
                env.get(
                    "ENDLESS_TASK_MAX_CHILD_HARD_SECONDS",
                    str(CHILD_DEFAULT_MAX_HARD_SECONDS),
                ),
                name="ENDLESS_TASK_MAX_CHILD_HARD_SECONDS",
            ),
            # M3-3：子运行成本上限。用"非负"解析（0 合法 = 关闭上界），与墙上限的
            # "正数"解析刻意不同——cap=0 不会把子运行压到地板，它就是"关掉"。
            max_child_cost_usd=_parse_non_negative_float(
                env.get(
                    "ENDLESS_TASK_MAX_CHILD_COST_USD",
                    str(CHILD_DEFAULT_COST_CAP_USD),
                ),
                name="ENDLESS_TASK_MAX_CHILD_COST_USD",
            ),
            # 切片 10 T1 Step 3：委派子运行的默认墙（模型不写 timeoutSeconds 时用它）。
            # 默认 120 = 今天写死在 handler 构造默认参数里的值。
            child_default_timeout_seconds=_parse_positive_float(
                env.get("ENDLESS_TASK_CHILD_DEFAULT_TIMEOUT_SECONDS", "120"),
                name="ENDLESS_TASK_CHILD_DEFAULT_TIMEOUT_SECONDS",
            ),
            # 切片 10 T1 Step 4：交互 run 档位选择器。只影响交互 run；非法值指名报错。
            run_budget_profile=_parse_run_budget_profile(
                env.get("ENDLESS_TASK_RUN_BUDGET_PROFILE", "interactive"),
            ),
            # M3 Task 7：在册（含排队）子运行的绝对上限。走带名解析：0/负数 不是
            # "关掉上限"的表达方式（那等于让所有子运行都派不出去）。
            max_queued_children=_parse_positive_int(
                env.get(
                    "ENDLESS_TASK_MAX_QUEUED_CHILDREN",
                    str(DEFAULT_MAX_QUEUED_CHILDREN),
                ),
                name="ENDLESS_TASK_MAX_QUEUED_CHILDREN",
            ),
            otel_export_endpoint=env.get(
                "ENDLESS_TASK_OTEL_ENDPOINT",
                "http://127.0.0.1:4318/v1/traces",
            ),
            artifact_proposals_enabled=_parse_flag(
                env.get("ENDLESS_TASK_ARTIFACT_PROPOSALS", "1")
            ),
            task_proposals_enabled=_parse_flag(
                env.get("ENDLESS_TASK_TASK_PROPOSALS", "1")
            ),
            knowledge_proposals_enabled=_parse_flag(
                env.get("ENDLESS_TASK_KNOWLEDGE_PROPOSALS", "1")
            ),
            knowledge_query_rewrite_enabled=_parse_flag(
                env.get("ENDLESS_TASK_KNOWLEDGE_QUERY_REWRITE", "0")
            ),
            knowledge_rerank_enabled=_parse_flag(
                env.get("ENDLESS_TASK_KNOWLEDGE_RERANK", "0")
            ),
            knowledge_scope_weights=_parse_json_mapping(
                env.get("ENDLESS_TASK_KNOWLEDGE_SCOPE_WEIGHTS", "")
            ),
            knowledge_synonym_map=_parse_json_mapping(
                env.get("ENDLESS_TASK_KNOWLEDGE_SYNONYMS", "")
            ),
            knowledge_duplicate_threshold=float(
                env.get("ENDLESS_TASK_KNOWLEDGE_DUPLICATE_THRESHOLD", "0.5")
            ),
            knowledge_decay_enabled=_parse_flag(
                env.get("ENDLESS_TASK_KNOWLEDGE_DECAY", "1")
            ),
            knowledge_decay_min_age_days=int(
                env.get("ENDLESS_TASK_KNOWLEDGE_DECAY_MIN_AGE_DAYS", "30")
            ),
            knowledge_decay_recheck_days=int(
                env.get("ENDLESS_TASK_KNOWLEDGE_DECAY_RECHECK_DAYS", "90")
            ),
            knowledge_decay_interval_hours=float(
                env.get("ENDLESS_TASK_KNOWLEDGE_DECAY_INTERVAL_HOURS", "24")
            ),
            knowledge_feedback_enabled=_parse_flag(
                env.get("ENDLESS_TASK_KNOWLEDGE_FEEDBACK", "1")
            ),
            embedding_enabled=_parse_flag(env.get("ENDLESS_TASK_EMBEDDING", "0")),
            embedding_backend=env.get(
                "ENDLESS_TASK_EMBEDDING_BACKEND", "local"
            ).strip().lower(),
            embedding_model=env.get("ENDLESS_TASK_EMBEDDING_MODEL", "").strip()
            or None,
            embedding_local_repo=env.get(
                "ENDLESS_TASK_EMBEDDING_LOCAL_REPO", "Xenova/bge-small-zh-v1.5"
            ).strip(),
            embedding_local_url_base=env.get(
                "ENDLESS_TASK_EMBEDDING_LOCAL_URL_BASE", "https://huggingface.co"
            ).strip(),
            embedding_max_chars=int(
                env.get("ENDLESS_TASK_EMBEDDING_MAX_CHARS", "1500")
            ),
            embedding_batch_size=int(env.get("ENDLESS_TASK_EMBEDDING_BATCH", "8")),
            embedding_sqlite_vec=_parse_flag(env.get("ENDLESS_TASK_SQLITE_VEC", "0")),
            hybrid_literal_weight=_parse_hybrid_weights(
                env.get("ENDLESS_TASK_KNOWLEDGE_HYBRID_WEIGHTS", "")
            )[0],
            hybrid_semantic_weight=_parse_hybrid_weights(
                env.get("ENDLESS_TASK_KNOWLEDGE_HYBRID_WEIGHTS", "")
            )[1],
            memory_recency_weight=_parse_unit_ratio(
                env.get("ENDLESS_TASK_MEMORY_RECENCY_WEIGHT", "0"),
                name="ENDLESS_TASK_MEMORY_RECENCY_WEIGHT",
            ),
            memory_decay_tau_days=_parse_positive_float(
                env.get("ENDLESS_TASK_MEMORY_DECAY_TAU", "30"),
                name="ENDLESS_TASK_MEMORY_DECAY_TAU",
            ),
            memory_forgetting_enabled=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_FORGETTING", "0")
            ),
            reflection_enabled=_parse_flag(
                env.get("ENDLESS_TASK_MEMORY_REFLECTION", "1")
            ),
            memory_forgetting_interval_hours=_parse_positive_float(
                env.get("ENDLESS_TASK_MEMORY_FORGETTING_INTERVAL_HOURS", "24"),
                name="ENDLESS_TASK_MEMORY_FORGETTING_INTERVAL_HOURS",
            ),
            memory_forgetting_max_per_run=int(
                env.get("ENDLESS_TASK_MEMORY_FORGETTING_MAX_PER_RUN", "10")
            ),
            memory_consolidation_threshold=_parse_ratio(
                env.get("ENDLESS_TASK_MEMORY_CONSOLIDATION_THRESHOLD", "0.5"),
                name="ENDLESS_TASK_MEMORY_CONSOLIDATION_THRESHOLD",
            ),
            memory_consolidation_max_per_run=int(
                env.get("ENDLESS_TASK_MEMORY_CONSOLIDATION_MAX_PER_RUN", "3")
            ),
            proposal_daily_budget=int(
                env.get("ENDLESS_TASK_PROPOSAL_DAILY_BUDGET", "6")
            ),
            proposal_cooldown_minutes=int(
                env.get("ENDLESS_TASK_PROPOSAL_COOLDOWN_MINUTES", "30")
            ),
            proposal_quiet_start=env.get(
                "ENDLESS_TASK_PROPOSAL_QUIET_START", "23:00"
            ).strip(),
            proposal_quiet_end=env.get(
                "ENDLESS_TASK_PROPOSAL_QUIET_END", "07:00"
            ).strip(),
            scheduler_enabled=_parse_flag(env.get("ENDLESS_TASK_SCHEDULER", "1")),
            task_run_review_enabled=_parse_flag(
                env.get("ENDLESS_TASK_RUN_REVIEW", "1")
            ),
            notifications_enabled=_parse_flag(
                env.get("ENDLESS_TASK_NOTIFICATIONS", "1")
            ),
            task_max_attempts=int(env.get("ENDLESS_TASK_TASK_MAX_ATTEMPTS", "3")),
            task_retry_backoff_seconds=float(
                env.get("ENDLESS_TASK_TASK_RETRY_BACKOFF", "60")
            ),
            shell_timeout_seconds=float(
                env.get("ENDLESS_TASK_SHELL_TIMEOUT_SECONDS", "120")
            ),
            # 切片 10 T1 Step 2：带名解析（负数 / nan / inf 不再是裸 float() 的
            # "could not convert" 或静默放行）。0 = 关闭看门狗，是**合法**值，
            # 所以用 `_parse_non_negative_float` 而不是正数版；下限钳制在
            # `__post_init__` 里统一做。
            shell_no_change_timeout_seconds=_parse_non_negative_float(
                env.get("ENDLESS_TASK_SHELL_NO_CHANGE_TIMEOUT_SECONDS", "60"),
                name="ENDLESS_TASK_SHELL_NO_CHANGE_TIMEOUT_SECONDS",
            ),
            shell_max_output_bytes=int(
                env.get("ENDLESS_TASK_SHELL_MAX_OUTPUT_BYTES", "65536")
            ),
            workspace_max_write_bytes=int(
                env.get("ENDLESS_TASK_WORKSPACE_MAX_WRITE_BYTES", "512000")
            ),
            scheduler_tick_seconds=float(
                env.get("ENDLESS_TASK_SCHEDULER_TICK", "30")
            ),
            config_version=int(env.get("ENDLESS_TASK_CONFIG_VERSION", "1")),
            provider_name=provider_name,
            model=env.get("ENDLESS_TASK_MODEL", default_model),
            base_url=env.get("ENDLESS_TASK_BASE_URL", default_base_url),
            api_key=api_key,
            provider_timeout_seconds=float(
                env.get("ENDLESS_TASK_PROVIDER_TIMEOUT_SECONDS", "60")
            ),
            system_prompt=env.get(
                "ENDLESS_TASK_SYSTEM_PROMPT",
                DEFAULT_SYSTEM_PROMPT,
            ),
            system_prompt_version=env.get(
                "ENDLESS_TASK_SYSTEM_PROMPT_VERSION",
                "p1-v3",
            ),
            context_window_tokens=int(
                env.get("ENDLESS_TASK_CONTEXT_WINDOW_TOKENS", "131072")
            ),
            max_output_tokens=int(
                env.get("ENDLESS_TASK_MAX_OUTPUT_TOKENS", "4096")
            ),
            summary_token_limit=int(
                env.get("ENDLESS_TASK_SUMMARY_TOKEN_LIMIT", "1024")
            ),
            max_concurrent_model_calls=int(
                env.get("ENDLESS_TASK_MAX_CONCURRENT_MODEL_CALLS", "2")
            ),
            max_concurrent_task_runs=int(
                env.get("ENDLESS_TASK_MAX_TASK_RUNS", "1")
            ),
            max_tool_calls_per_turn=int(
                env.get("ENDLESS_TASK_MAX_TOOL_CALLS_PER_TURN", "8")
            ),
            max_concurrent_tool_calls=int(
                env.get("ENDLESS_TASK_MAX_CONCURRENT_TOOL_CALLS", "4")
            ),
            max_tool_argument_bytes=int(
                env.get("ENDLESS_TASK_MAX_TOOL_ARGUMENT_BYTES", "65536")
            ),
            max_total_tool_argument_bytes=int(
                env.get("ENDLESS_TASK_MAX_TOTAL_TOOL_ARGUMENT_BYTES", "262144")
            ),
            max_tool_argument_depth=int(
                env.get("ENDLESS_TASK_MAX_TOOL_ARGUMENT_DEPTH", "32")
            ),
            max_tool_argument_nodes=int(
                env.get("ENDLESS_TASK_MAX_TOOL_ARGUMENT_NODES", "10000")
            ),
            # 哨兵语义：只有在环境里存在时才覆盖交互式档的 hard（否则 None → 用默认 900）。
            # 走带名解析：裸 float() 对非数字的报错不指名变量，nan/inf 更会被静默采纳。
            agent_timeout_seconds=(
                _parse_positive_float(
                    env["ENDLESS_TASK_AGENT_TIMEOUT_SECONDS"],
                    name="ENDLESS_TASK_AGENT_TIMEOUT_SECONDS",
                )
                if "ENDLESS_TASK_AGENT_TIMEOUT_SECONDS" in env
                else None
            ),
            approval_timeout_seconds=float(
                env.get("ENDLESS_TASK_APPROVAL_TIMEOUT_SECONDS", "1800")
            ),
            max_message_characters=int(
                env.get("ENDLESS_TASK_MAX_MESSAGE_CHARACTERS", "100000")
            ),
            max_file_bytes=int(
                env.get("ENDLESS_TASK_MAX_FILE_BYTES", "1000000")
            ),
            max_files_per_conversation=int(
                env.get("ENDLESS_TASK_MAX_FILES_PER_CONVERSATION", "10")
            ),
        )


def _parse_skill_install_mode(raw: str) -> str:
    """S8：`ENDLESS_TASK_SKILL_INSTALL=ask|allow|deny`；非法值启动即失败。"""
    value = (raw or "ask").strip().lower()
    if value not in ("ask", "allow", "deny"):
        raise ValueError(
            "ENDLESS_TASK_SKILL_INSTALL 只能是 ask / allow / deny。"
        )
    return value


def _parse_flag(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_strict_flag(value: str, *, name: str) -> bool:
    """Strict boolean env parsing: any value outside 0/1 fails startup."""
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 只允许 0 或 1")


def _parse_positive_int(value: str, *, name: str) -> int:
    """Strict positive int parsing（切片 9 M2：唤醒在飞上限）。

    `int()` 本身就把 "1.5" / "nan" / "" 挡在门外（启动即失败），所以这里只需要
    再挡住 <= 0——"上限 0" 会让唤醒链路静默失效，不是"关掉"（关掉走开关）。
    """
    try:
        parsed = int(value.strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是正整数") from error
    if parsed <= 0:
        raise ValueError(f"{name} 必须是正整数")
    return parsed


def _parse_positive_float(value: str, *, name: str) -> float:
    """Strict positive float parsing (B1 decay tau)."""
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是正数") from error
    # 有限性必须显式检查：nan 会绕过下面所有比较（与 nan 的比较恒为 False），
    # inf 则让"上限"名存实亡——预算类字段写 inf 等于静默关掉墙。
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError(f"{name} 必须是正数")
    return parsed


def _parse_unit_ratio(value: str, *, name: str) -> float:
    """Strict [0, 1] ratio parsing (0 = feature off)."""
    parsed = _parse_non_negative_float(value, name=name)
    if parsed > 1:
        raise ValueError(f"{name} 必须在 [0, 1] 之间")
    return parsed


def _parse_non_negative_float(value: str, *, name: str) -> float:
    """Strict non-negative float parsing (0 disables the limit)."""
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是非负数字") from error
    # 同 _parse_positive_float：nan 绕过比较、inf 让上限失效（成本断路器会静默
    # 关闭），两者都不是"合法数字"。
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{name} 必须是非负数字")
    return parsed


def _parse_verifier_mode(value: str) -> str:
    """Strict verifier mode parsing (C1): 0 | 1 | side_effects."""
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return "0"
    if normalized in {"1", "true", "yes", "on", "all"}:
        return "1"
    if normalized in {"side_effects", "side-effects"}:
        return "side_effects"
    raise ValueError("ENDLESS_TASK_VERIFIER 只允许 0 | 1 | side_effects")


def _parse_ratio(value: str, *, name: str) -> float:
    """Strict (0, 1] ratio parsing: illegal env values fail startup."""
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是 (0, 1] 之间的小数") from error
    if not 0 < parsed <= 1:
        raise ValueError(f"{name} 必须是 (0, 1] 之间的小数")
    return parsed


def _parse_delegation_mode(value: str) -> str:
    """Strict delegation mode parsing (06 §9): 0 | readonly | isolated_write.

    ``isolated_write`` (M4B) is accepted from P2a on; startup additionally
    requires the execution-backend enforcement seam to be enabled (checked in
    the composition root) — never silently degrades to read-only.
    """
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return "0"
    if normalized in {"1", "true", "yes", "on", "readonly"}:
        return "readonly"
    if normalized == "isolated_write":
        return "isolated_write"
    raise ValueError("ENDLESS_TASK_DELEGATION 只允许 0 | readonly | isolated_write")


def _parse_execution_backend_mode(value: str) -> str:
    """Strict execution-backend mode parsing (M3B slice F): 0|local|container.

    "" = enforcement off (current direct tool behavior). ``local``/``container``
    both route unattended fs mutations through the ExecutionEnvironment (the
    container backend executes file mutations locally too — its isolation
    difference applies to process execution, which unattended runs cannot
    reach), so either value is an explicit opt-in to the enforcement seam;
    illegal values fail startup.
    """
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off", ""}:
        return ""
    if normalized in {"1", "true", "yes", "on", "local"}:
        return "local"
    if normalized == "container":
        return "container"
    raise ValueError("ENDLESS_TASK_EXECUTION_BACKEND 只允许 0|local|container")


def _parse_run_budget_profile(value: str) -> str:
    """Strict interactive-run budget profile parsing（切片 10 T1 Step 4）。

    取值域就是 `RUN_BUDGET_PROFILES`（= `resolve_run_budget` 认识的三个运行类别）。
    非法值**装配期指名报错**，不静默回落到 `interactive`：静默回落会让"我配了
    long 却没生效"变成看不见的洞——而那正是这个设置要修的毛病本身。

    大小写与空白宽容（与 `_parse_delegation_mode` / `_parse_execution_backend_mode`
    一致）：``LONG`` / `` long `` 都接受，写错的名字才报错。
    """
    normalized = value.strip().lower()
    if normalized in RUN_BUDGET_PROFILES:
        return normalized
    raise ValueError(
        "ENDLESS_TASK_RUN_BUDGET_PROFILE 只允许 " + " | ".join(RUN_BUDGET_PROFILES)
    )


def _parse_strict_mode(value: str, *, name: str) -> str:
    """Strict 0|1 mode parsing: any value outside 0/1 fails startup."""
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return "1"
    if normalized in {"0", "false", "no", "off"}:
        return "0"
    raise ValueError(f"{name} 只允许 0 或 1")


def _parse_runtime_trace_mode(value: str) -> str:
    """Strict runtime trace mode parsing (08 §OE-1): 0|errors|sampled|all."""
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off"}:
        return "0"
    if normalized in {"errors"}:
        return "errors"
    if normalized in {"sampled"}:
        return "sampled"
    if normalized in {"1", "true", "yes", "on", "all"}:
        return "all"
    raise ValueError("ENDLESS_TASK_RUNTIME_TRACE 只允许 0|errors|sampled|all")


def _parse_otel_export_mode(value: str) -> str:
    """Strict OTLP export mode parsing (08 §OE-5): 0|otlp-http."""
    normalized = value.strip().lower()
    if normalized in {"0", "false", "no", "off", ""}:
        return "0"
    if normalized in {"1", "true", "yes", "on", "otlp", "otlp-http"}:
        return "otlp-http"
    raise ValueError("ENDLESS_TASK_OTEL_EXPORT 只允许 0|otlp-http")


def _parse_hybrid_weights(value: str) -> tuple[float, float]:
    text = (value or "").strip()
    if not text:
        return (0.4, 0.6)
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 2:
        raise ValueError(
            "ENDLESS_TASK_KNOWLEDGE_HYBRID_WEIGHTS expects 'literal,semantic'"
        )
    literal, semantic = float(parts[0]), float(parts[1])
    if literal < 0 or semantic < 0:
        raise ValueError("Hybrid weights cannot be negative")
    return literal, semantic


def _parse_json_mapping(value: str) -> dict:
    text = (value or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"Invalid JSON mapping config: {value!r}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"Expected a JSON object, got: {value!r}")
    return parsed
