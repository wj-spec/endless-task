"""应用设置（从 app.py 搬出；默认值、环境变量名与解析规则零改动）。

`AppSettings` 及其环境解析 helper 是纯配置层，不依赖容器，因此可以独立成模块。
`api/app.py` 仍 re-export `AppSettings`，既有 `from endless_task.api.app import AppSettings`
导入路径继续可用。
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from endless_task.runtime_v2.verification import VERIFIER_MODES


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
    agent_timeout_seconds: float = 120.0
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
    task_max_attempts: int = 3
    task_retry_backoff_seconds: float = 60.0
    shell_timeout_seconds: float = 120.0
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
    # C5: 单次运行的估算成本上限（USD，0 = 不限制）；超限走 C4 升级提示。
    cost_cap_usd: float = 0.0

    def __post_init__(self) -> None:
        if self.config_version != CONFIG_VERSION:
            raise ValueError(
                f"Unsupported config version {self.config_version}; expected {CONFIG_VERSION}"
            )
        if self.provider_timeout_seconds <= 0 or self.heartbeat_seconds <= 0:
            raise ValueError("Timeout values must be positive")
        if self.context_window_tokens <= self.max_output_tokens:
            raise ValueError("Context window must be larger than max output tokens")
        if self.cost_cap_usd < 0:
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
        if self.agent_timeout_seconds <= 0:
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
            cost_cap_usd=_parse_non_negative_float(
                env.get("ENDLESS_TASK_COST_CAP_USD", "0"),
                name="ENDLESS_TASK_COST_CAP_USD",
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
            shell_no_change_timeout_seconds=float(
                env.get("ENDLESS_TASK_SHELL_NO_CHANGE_TIMEOUT_SECONDS", "60")
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
            agent_timeout_seconds=float(
                env.get("ENDLESS_TASK_AGENT_TIMEOUT_SECONDS", "120")
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


def _parse_positive_float(value: str, *, name: str) -> float:
    """Strict positive float parsing (B1 decay tau)."""
    try:
        parsed = float(value.strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是正数") from error
    if parsed <= 0:
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
    if parsed < 0:
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
