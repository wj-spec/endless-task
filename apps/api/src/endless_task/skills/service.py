from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Sequence, Tuple
from xml.sax.saxutils import escape

from ..storage.sqlite_skill_override_repository import SqliteSkillOverrideRepository
from .loader import (
    SkillRootSpec,
    default_skill_root_specs,
    discover_from_roots,
    discover_skills,
)
from .models import Skill, SkillScope

#: 单次注入的技能正文上限（字节）；超出按字符安全截断并标注。
MAX_SKILL_BODY_BYTES = 32 * 1024

#: 显式调用失败原因（供 API 与 UI 复用）。
INVOKE_OK = "ok"
INVOKE_UNKNOWN = "unknown"
INVOKE_NOT_USER_INVOCABLE = "not_user_invocable"
INVOKE_DISABLED = "disabled"
INVOKE_INVALID = "invalid"


#: S3：目录里单条描述的上限（避免一个技能吃掉整个前缀预算）。
MAX_CATALOG_DESCRIPTION_CHARACTERS = 500

#: S5：默认目录预算（见 05 文档）——展开条目数 / 单条摘要 / 总字符。
DEFAULT_CATALOG_LIMIT = 8
DEFAULT_CATALOG_SUMMARY_CHARACTERS = 160
DEFAULT_CATALOG_BUDGET_CHARACTERS = 3_000
#: 兼容旧调用方（不再用于默认路径）。
MAX_CATALOG_CHARACTERS = 12_000
_CATALOG_DESCRIPTION_STEPS = (500, 240, 160, 100, 60)

#: "最近常用"的时间窗。
_RECENT_USAGE_DAYS = 30


def _render_catalog(
    visible: Tuple[Skill, ...],
    *,
    folded: int,
    summary_characters: int,
    budget: int,
) -> str:
    """S5：默认目录渲染——精选条目 + 折叠时的能力感知行。"""
    lines = [
        "以下技能为特定任务提供专门指引（这里只有摘要，未读取正文前不要据其行动）。"
        "任务与某技能描述匹配时，先调用 read_skill_file（传 name）读取全文，"
        "再按其中步骤执行；技能内相对路径以其所在目录解析。",
        "<available_skills>",
    ]
    for skill in visible:
        description = skill.description.strip()
        if len(description) > summary_characters:
            description = description[:summary_characters].rstrip() + "…"
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(skill.name)}</name>",
                f"    <description>{escape(description)}</description>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    if folded > 0:
        lines.append(
            f"本地还有 {folded} 个技能未列出。需要特定领域做法时，先用 skill_search "
            "在「工作区 / 全局」检索（含 ~/.claude/skills 等共享目录）找出技能名，"
            "再用 read_skill_file(name) 读正文；本地确实没有时，可以提议从生态安装"
            "（安装需要用户确认）。用户用 /技能名 指定的技能会直接加载，不必检索。"
        )
    text = "\n".join(lines)
    if len(text) > budget and folded > 0:
        # 兜底：只保留能力感知行 + 最前面的条目，绝不超预算。
        head = "\n".join(lines[: len(lines) - 1])
        trimmed: list[str] = []
        for line in lines[2 : len(lines) - 1]:
            attempt = "\n".join([*lines[:2], *trimmed, line, lines[-2]])
            if len(attempt) > budget - len(lines[-1]) - 2:
                break
            trimmed.append(line)
        del head
        text = "\n".join([*lines[:2], *trimmed, lines[-2], lines[-1]])
    return text


def _iso_days_ago(days: int, *, now: Optional[str] = None) -> str:
    from datetime import datetime, timedelta, timezone

    if now:
        return now
    moment = datetime.now(timezone.utc) - timedelta(days=max(0, days))
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_available_skills_prompt(
    skills: Tuple[Skill, ...],
    *,
    locator_mode: bool = False,
    folded: int = 0,
    summary_characters: int = DEFAULT_CATALOG_SUMMARY_CHARACTERS,
    budget: int = DEFAULT_CATALOG_BUDGET_CHARACTERS,
    legacy_steps: bool = False,
) -> str:
    """技能目录：**只给名称 + 截断描述，不给路径**（S3）。

    模型通过 ``read_skill_file(name=...)`` 按需读取正文；路径是本地实现细节，
    既不该进入模型上下文，也会在技能移动/换工作区后失效。
    """
    del locator_mode  # 兼容旧调用方；目录不再暴露路径或 locator。
    visible = [skill for skill in skills if skill.valid and not skill.disable_model_invocation]
    if not visible:
        return ""
    if not legacy_steps:
        return _render_catalog(
            tuple(visible),
            folded=folded,
            summary_characters=summary_characters,
            budget=budget,
        )

    def render(entries: tuple[Skill, ...], description_cap: int) -> str:
        lines = [
            "以下技能为特定任务提供专门指引（这里只有摘要，未读取正文前不要据其行动）。"
            "任务与某技能描述匹配时，先调用 read_skill_file（传 name）读取全文，"
            "再按其中步骤执行；技能内相对路径以其所在目录解析。",
            "<available_skills>",
        ]
        for skill in entries:
            description = skill.description.strip()
            if len(description) > description_cap:
                description = description[:description_cap].rstrip() + "…"
            lines.extend(
                [
                    "  <skill>",
                    f"    <name>{escape(skill.name)}</name>",
                    f"    <description>{escape(description)}</description>",
                    "  </skill>",
                ]
            )
        lines.append("</available_skills>")
        return "\n".join(lines)

    cap = MAX_CATALOG_DESCRIPTION_CHARACTERS
    for candidate_cap in _CATALOG_DESCRIPTION_STEPS:
        cap = candidate_cap
        text = render(tuple(visible), cap)
        if len(text) <= MAX_CATALOG_CHARACTERS:
            return text
    # 描述压到最小仍超预算：保留前 N 个，其余提示用 /技能名 显式调用。
    kept: list[Skill] = []
    for skill in visible:
        attempt = render(tuple([*kept, skill]), cap)
        if len(attempt) > MAX_CATALOG_CHARACTERS - 160:
            break
        kept.append(skill)
    remaining = len(visible) - len(kept)
    text = render(tuple(kept), cap)
    if remaining > 0:
        text += (
            f"\n（另有 {remaining} 个技能未列出：可用 /技能名 显式调用，"
            "或让用户说明任务后按需提供。）"
        )
    return text


class SkillService:
    def __init__(
        self,
        *,
        user_dir: Path,
        database_path: Path,
        override_repository: SqliteSkillOverrideRepository,
        extra_skill_dirs: Sequence[Path] = (),
        home_dir: Optional[Path] = None,
        usage_repository=None,
    ) -> None:
        self._user_dir = user_dir
        self._database_path = database_path
        self._override_repository = override_repository
        #: S4：除应用目录外的共享技能根（~/.claude/skills 等 + 环境变量追加）。
        self._extra_skill_dirs = tuple(extra_skill_dirs)
        #: S5：使用统计（决定"最近常用"进入默认目录）。
        self._usage_repository = usage_repository
        #: 共享根使用的 home；显式传入优先，其次 `ENDLESS_TASK_SKILL_HOME`（测试隔离）。
        override = (os.environ.get("ENDLESS_TASK_SKILL_HOME") or "").strip()
        self._home_dir = (
            home_dir
            if home_dir is not None
            else (Path(override).expanduser() if override else None)
        )

    def root_specs(self, workspace_root: Optional[Path] = None) -> Tuple[SkillRootSpec, ...]:
        """S4：技能根清单（工作区 → 全局共享 → 应用自带）。"""
        return default_skill_root_specs(
            user_dir=self._user_dir,
            workspace_root=workspace_root,
            extra_dirs=self._extra_skill_dirs,
            home=self._home_dir,
        )

    def list_skills(
        self,
        workspace_root: Optional[Path] = None,
        *,
        workspace_id: str = "",
    ) -> Tuple[Skill, ...]:
        skills = discover_from_roots(self.root_specs(workspace_root))
        disabled = self._override_repository.disabled_index()
        user_disabled = disabled.get((SkillScope.USER.value, ""), set())
        workspace_disabled = disabled.get(
            (SkillScope.WORKSPACE.value, workspace_id), set()
        )
        pinned = self._override_repository.pinned_index()
        user_pinned = pinned.get((SkillScope.USER.value, ""), set())
        workspace_disabled_pinned = pinned.get(
            (SkillScope.WORKSPACE.value, workspace_id), set()
        )
        return tuple(
            Skill(
                name=skill.name,
                description=skill.description,
                scope=skill.scope,
                file_path=skill.file_path,
                digest=skill.digest,
                version=skill.version,
                when_to_use=skill.when_to_use,
                source=skill.source,
                source_rank=skill.source_rank,
                pinned=(
                    skill.name in (
                        workspace_disabled_pinned
                        if skill.scope == SkillScope.WORKSPACE
                        else user_pinned
                    )
                ),
                disabled=skill.name in (
                    workspace_disabled if skill.scope == SkillScope.WORKSPACE else user_disabled
                ),
                disable_model_invocation=skill.disable_model_invocation,
                model_invocable=skill.model_invocable,
                user_invocable=skill.user_invocable,
                diagnostics=skill.diagnostics,
            )
            for skill in skills
        )

    def set_disabled(
        self,
        *,
        scope: SkillScope,
        name: str,
        disabled: bool,
        workspace_id: str = "",
    ) -> None:
        self._override_repository.set_disabled(
            scope=scope.value,
            workspace_id=workspace_id,
            name=name,
            disabled=disabled,
        )

    def set_pinned(
        self,
        *,
        scope: SkillScope,
        name: str,
        pinned: bool,
        workspace_id: str = "",
    ) -> None:
        """S5：固定/取消固定进默认目录。"""
        self._override_repository.set_pinned(
            scope=scope.value,
            workspace_id="" if scope is SkillScope.USER else workspace_id,
            name=name,
            pinned=pinned,
        )

    def visible_skills(
        self,
        workspace_root: Optional[Path] = None,
        *,
        workspace_id: str = "",
    ) -> Tuple[Skill, ...]:
        return tuple(
            skill
            for skill in self.list_skills(
                workspace_root, workspace_id=workspace_id
            )
            if skill.valid and not skill.disabled
        )

    def catalog_skills(
        self,
        workspace_root: Optional[Path] = None,
        *,
        workspace_id: str = "",
        limit: int = DEFAULT_CATALOG_LIMIT,
        recent_days: int = _RECENT_USAGE_DAYS,
        now: Optional[str] = None,
    ) -> Tuple[Tuple[Skill, ...], int]:
        """S5：挑出进默认目录的精选技能，返回 (精选, 被折叠数量)。

        规则（05 §3.1）：工作区技能 > 用户固定 > 最近常用（invoked/body_read），
        同级按使用次数降序、名称升序；其余折叠但不删除——
        仍可被 ``/技能名`` 显式调用或 ``skill_search`` 检索到。
        """
        candidates = [
            skill
            for skill in self.visible_skills(
                workspace_root, workspace_id=workspace_id
            )
            if not skill.disable_model_invocation
        ]
        if not candidates:
            return (), 0
        pinned = self._override_repository.pinned_index()
        pinned_names = pinned.get((SkillScope.USER.value, ""), set()) | pinned.get(
            (SkillScope.WORKSPACE.value, workspace_id), set()
        )
        since = _iso_days_ago(recent_days, now=now)
        activity = self._usage_activity(since)

        def activity_count(skill: Skill) -> int:
            return activity.get((skill.scope.value, skill.name), (0, ""))[0]

        # 必进项：工作区技能（项目专属）+ 用户固定（用户意图优先于统计）。
        # 它们不受 limit 挤占，只受一个安全上限约束，避免目录重新膨胀。
        hard_max = max(limit, limit * 2)
        always = [
            skill
            for skill in candidates
            if skill.scope is SkillScope.WORKSPACE or skill.name in pinned_names
        ]
        always.sort(key=lambda skill: (0 if skill.scope is SkillScope.WORKSPACE else 1, skill.name))
        always = always[:hard_max]
        always_names = {skill.name for skill in always}

        rest = sorted(
            (skill for skill in candidates if skill.name not in always_names),
            key=lambda skill: (-activity_count(skill), skill.name),
        )
        remaining_slots = max(0, max(1, limit) - len(always))
        selected = tuple([*always, *rest[:remaining_slots]])
        return selected, max(0, len(candidates) - len(selected))

    def _usage_activity(self, since: str):
        repository = getattr(self, "_usage_repository", None)
        if repository is None:
            return {}
        try:
            return repository.activity_index(since=since)
        except Exception:  # noqa: BLE001 统计不可用不影响目录生成
            return {}

    def invocable_skills(
        self,
        workspace_root: Optional[Path] = None,
        *,
        workspace_id: str = "",
    ) -> Tuple[Skill, ...]:
        """S1：用户可显式调用的技能（与 model-invocable 解耦）。"""
        return tuple(
            skill
            for skill in self.visible_skills(workspace_root, workspace_id=workspace_id)
            if skill.user_invocable
        )

    def resolve_invocable(
        self,
        name: str,
        workspace_root: Optional[Path] = None,
        *,
        workspace_id: str = "",
    ) -> Tuple[Optional[Skill], str]:
        """解析显式调用：返回 (技能, 原因码)。

        名称根本不存在时返回 ``unknown``（普通文本里的 ``/tmp`` 不算错误）；
        存在但不允许用户调用/被禁用/清单非法时返回对应原因。
        """
        target = (name or "").strip()
        if not target:
            return None, INVOKE_UNKNOWN
        for skill in self.list_skills(workspace_root, workspace_id=workspace_id):
            if skill.name != target:
                continue
            if not skill.valid:
                return None, INVOKE_INVALID
            if skill.disabled:
                return None, INVOKE_DISABLED
            if not skill.user_invocable:
                return None, INVOKE_NOT_USER_INVOCABLE
            return skill, INVOKE_OK
        return None, INVOKE_UNKNOWN

    def skill_body(self, skill: Skill, *, max_bytes: int = MAX_SKILL_BODY_BYTES) -> str:
        """读取技能正文（去 frontmatter，有界，按字符安全截断）。"""
        try:
            text = skill.file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""
        # 注入给模型的是"正文"，不含 frontmatter（模型不需要看清单字段）。
        try:
            from .manifest import parse_skill_manifest

            parsed = parse_skill_manifest(skill.file_path, text)
            if parsed.body.strip():
                text = parsed.body
        except Exception:  # noqa: BLE001 解析失败时退回原文
            pass
        raw = text.encode("utf-8")
        if len(raw) <= max_bytes:
            return text
        # 按字符边界裁剪，避免切坏多字节字符。
        clipped = raw[:max_bytes]
        while clipped:
            try:
                return clipped.decode("utf-8") + "\n\n[技能正文已截断]"
            except UnicodeDecodeError:
                clipped = clipped[:-1]
        return ""

    def skill_roots(self, workspace_root: Optional[Path] = None) -> tuple[Path, ...]:
        """所有技能根（含共享目录）：读取正文时的根包含性校验用。"""
        return tuple(spec.path for spec in self.root_specs(workspace_root))
