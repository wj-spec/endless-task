"""技能域路由（从 app.py 搬出；路径、状态码、响应体零改动）。

这是"搬家"而不是重构：每个 handler 依旧闭包在 `container` 上，位置参数与
搬家前完全一致；区别只有文件边界——以后技能相关的改动不必在 8 000 行的
`app.py` 里翻找。见 docs/product-improvements/05-code-health/01-monolith-audit.md。
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Query

from endless_task.domain.repositories import NotFoundError
from endless_task.skills import SkillScope

from ..container import AppContainer
from ..errors import ApiRequestError
from ..schemas.skills import (
    SkillCasesRunBody,
    SkillCreateBody,
    SkillImportBody,
    SkillInstallBody,
    SkillPatchBody,
    SkillSearchBody,
    SkillValidateBody,
)


def register_skill_routes(app: FastAPI, container: AppContainer) -> None:
        @app.get("/skills")
        async def list_skills(
            workspace: Optional[str] = Query(None),
        ) -> dict[str, object]:
            root = None
            if workspace:
                try:
                    item = container.workspace_repository.get_workspace(workspace)
                    root = (
                        Path(item.root_path).expanduser()
                        if item.root_path
                        else None
                    )
                except Exception:
                    root = None
            items = container.skill_service.list_skills(
                root, workspace_id=workspace or ""
            )
            catalog_names = {
                skill.name
                for skill in container.skill_service.catalog_skills(
                    root,
                    workspace_id=workspace or "",
                    limit=container.settings.skill_catalog_limit,
                )[0]
            }
            return {
                "userSkillsDirectory": str(
                    container.settings.database_path.parent / "skills"
                ),
                "items": [
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "scope": skill.scope.value,
                        "filePath": str(skill.file_path),
                        "version": skill.version,
                        "digest": skill.digest,
                        "whenToUse": skill.when_to_use,
                        "source": skill.source,
                        "pinned": skill.pinned,
                        # 是否出现在模型的默认目录里（未出现仍可用 /技能名 或 skill_search）
                        "inCatalog": skill.name in catalog_names,
                        "provenance": (
                            lambda item: item.as_dict() if item is not None else None
                        )(
                            container.skill_provenance_repository.get(
                                scope=skill.scope.value,
                                workspace_id=(
                                    ""
                                    if skill.scope is SkillScope.USER
                                    else (workspace or "")
                                ),
                                name=skill.name,
                            )
                        ),
                        "disabled": skill.disabled,
                        "disableModelInvocation": skill.disable_model_invocation,
                        "diagnostics": [
                            {
                                "code": item.code,
                                "message": item.message,
                                "path": str(item.path),
                            }
                            for item in skill.diagnostics
                        ],
                    }
                    for skill in items
                ],
            }

        def _skill_root_for(
            scope: str, workspace_id: Optional[str]
        ) -> Path:
            """S2：技能根目录（user = 数据目录/skills，workspace = <root>/.endless-task/skills）。"""
            if scope == "user":
                return container.settings.database_path.parent / "skills"
            if not workspace_id:
                raise ApiRequestError(
                    "invalid_request", "工作区级技能需要 workspaceId。"
                )
            try:
                workspace = container.workspace_repository.get_workspace(workspace_id)
            except NotFoundError as error:
                raise NotFoundError(f"Unknown workspace: {workspace_id}") from error
            if not workspace.root_path:
                raise ApiRequestError(
                    "workspace_not_bound", "该工作区未绑定本地目录。"
                )
            return Path(workspace.root_path).expanduser() / ".endless-task" / "skills"

        def _workspace_root_path(workspace_id: Optional[str]) -> Optional[Path]:
            if not workspace_id:
                return None
            try:
                workspace = container.workspace_repository.get_workspace(workspace_id)
            except NotFoundError as error:
                raise NotFoundError(f"Unknown workspace: {workspace_id}") from error
            return (
                Path(workspace.root_path).expanduser()
                if workspace.root_path
                else None
            )

        def _skill_package_dir(
            scope: str, name: str, workspace_id: Optional[str]
        ) -> tuple[Path, str, bool]:
            """定位技能包目录。

            S4：技能可能来自共享目录（~/.claude/skills 等），所以按**发现结果**定位，
            而不是拼应用技能目录。返回 (包目录, 来源标签, 是否可写)。

            可写 = 应用技能目录或工作区技能根；共享目录里的技能只读管理
            （避免误删其它 agent 正在用的技能）。
            """
            workspace_root = _workspace_root_path(workspace_id)
            target_scope = SkillScope(scope)
            for skill in container.skill_service.list_skills(
                workspace_root, workspace_id=workspace_id or ""
            ):
                if skill.name != name or skill.scope is not target_scope:
                    continue
                package = skill.file_path.parent
                label = skill.source or str(package)
                writable = _skill_source_is_writable(skill.source, label)
                return package, label, writable
            raise ApiRequestError(
                "skill_not_found", f"技能不存在：{scope}/{name}。", status_code=404
            )

        def _skill_source_is_writable(source: str, label: str) -> bool:
            """共享目录（~/.claude/skills 等）与工作区外目录视为只读。"""
            if not source:
                return True
            if source.startswith("~/"):
                return False
            return source in {
                "应用技能目录",
                label,
            } and not source.startswith("~")

        def _skill_validation_payload(path: Path, text: Optional[str]) -> dict[str, object]:
            """解析 + 扫描一份技能包，返回 UI 可直接渲染的结果。"""
            from endless_task.skills import (
                ScanReport,
                SkillRevision,
                parse_skill_manifest,
                scan_skill_directory,
                scan_skill_revision,
            )

            if text is None:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    raise ApiRequestError(
                        "unreadable", "无法读取 SKILL.md。", status_code=400
                    ) from None
            manifest = parse_skill_manifest(path, text)
            revision = SkillRevision.from_manifest(manifest, scope="user", path=path)
            body_scan = scan_skill_revision(revision)
            directory_scan = (
                scan_skill_directory(path.parent)
                if path.parent.is_dir() and path.name == "SKILL.md"
                else ScanReport()
            )
            scan = ScanReport(findings=body_scan.findings + directory_scan.findings)
            return {
                "valid": manifest.valid and not scan.quarantined,
                "manifest": {
                    "name": manifest.name,
                    "description": manifest.description,
                    "version": manifest.version,
                    "schemaVersion": manifest.schema_version,
                    "digest": manifest.digest,
                    "modelInvocable": manifest.model_invocable,
                    "userInvocable": manifest.user_invocable,
                    "requiredTools": list(manifest.required_tools),
                    "requiredCapabilities": list(manifest.required_capabilities),
                    "conflictsWith": list(manifest.conflicts_with),
                },
                "diagnostics": [
                    {
                        "code": diagnostic.code,
                        "message": diagnostic.message,
                        "path": str(diagnostic.path),
                    }
                    for diagnostic in manifest.diagnostics
                ],
                "scan": {
                    "worstLevel": (
                        scan.worst_level.value if scan.worst_level is not None else None
                    ),
                    "quarantined": scan.quarantined,
                    "findings": [
                        {
                            "code": finding.code,
                            "level": finding.level.value,
                            "message": finding.message,
                            "path": str(finding.path),
                        }
                        for finding in scan.findings
                    ],
                },
            }

        @app.post("/skills/validate")
        async def validate_skill(body: SkillValidateBody) -> dict[str, object]:
            """S2：导入前校验（目录路径或内联内容）。"""
            if body.path and body.content is not None:
                raise ApiRequestError(
                    "invalid_request", "path 与 content 只能给一个。"
                )
            if body.content is not None:
                return _skill_validation_payload(
                    Path("inline") / "SKILL.md", body.content
                )
            if not body.path:
                raise ApiRequestError("invalid_request", "需要 path 或 content。")
            candidate = Path(body.path).expanduser()
            if candidate.is_dir():
                candidate = candidate / "SKILL.md"
            if not candidate.is_file():
                raise ApiRequestError(
                    "skill_not_found", "目录里没有 SKILL.md。", status_code=404
                )
            return _skill_validation_payload(candidate, None)

        @app.post("/skills/import", status_code=201)
        async def import_skill(body: SkillImportBody) -> dict[str, object]:
            """S2：本地目录导入（staging → 扫描门禁 → 原子激活）。"""
            from endless_task.skills import ImportFailure, import_skill_package

            source = Path(body.sourcePath).expanduser()
            target_root = _skill_root_for(body.scope, body.workspaceId)
            target_root.mkdir(parents=True, exist_ok=True)
            try:
                result = import_skill_package(
                    source, target_root, allow_upgrade=body.allowUpgrade
                )
            except ImportFailure as failure:
                raise ApiRequestError(
                    failure.code, failure.message, status_code=400
                ) from None
            return {
                "skill": {
                    "name": result.name,
                    "version": result.version,
                    "digest": result.digest,
                    "target": str(result.target),
                    "upgraded": result.upgraded,
                    "replacedDigest": result.replaced_digest,
                    "worstLevel": (
                        result.scan_report.worst_level.value
                        if result.scan_report is not None
                        and result.scan_report.worst_level is not None
                        else None
                    ),
                }
            }

        @app.post("/skills/create", status_code=201)
        async def create_skill(body: SkillCreateBody) -> dict[str, object]:
            """S2：按 v2 模板新建技能。"""
            name = body.name.strip()
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
                raise ApiRequestError(
                    "invalid_name", "技能名必须为小写字母、数字或连字符。"
                )
            if not body.description.strip() or not body.body.strip():
                raise ApiRequestError(
                    "invalid_request", "description 与 body 不能为空。"
                )
            root = _skill_root_for(body.scope, body.workspaceId)
            package = root / name
            if package.exists():
                raise ApiRequestError(
                    "skill_exists",
                    f"技能 {name} 已存在；如需覆盖请用导入并确认升级。",
                    status_code=409,
                )
            package.mkdir(parents=True)
            lines = [
                "---",
                f"name: {name}",
                f"description: {body.description.strip()}",
                "version: 0.1.0",
                "schema-version: 2",
            ]
            if body.whenToUse and body.whenToUse.strip():
                lines.append(f"whenToUse: {body.whenToUse.strip()}")
            lines.append(f"model-invocable: {'true' if body.modelInvocable else 'false'}")
            lines.append(f"user-invocable: {'true' if body.userInvocable else 'false'}")
            lines.extend(["---", "", body.body.strip(), ""])
            (package / "SKILL.md").write_text("\n".join(lines), encoding="utf-8")
            return {
                "skill": {
                    "name": name,
                    "path": str(package / "SKILL.md"),
                    "scope": body.scope,
                }
            }

        @app.delete("/skills/{scope}/{name}")
        async def delete_skill(
            scope: Literal["user", "workspace"],
            name: str,
            workspace: Optional[str] = Query(None),
        ) -> dict[str, object]:
            """S2：删除技能包（移入同根 ``.trash``，可手工找回）。"""
            import shutil
            from datetime import datetime, timezone

            package, source_label, writable = _skill_package_dir(
                scope, name, workspace
            )
            if not writable:
                raise ApiRequestError(
                    "skill_read_only",
                    f"该技能来自共享目录（{source_label}），请直接在文件系统里管理。",
                    status_code=400,
                )
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            trash = package.parent / ".trash" / f"{stamp}-{name}"
            trash.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(package), str(trash))
            return {"deleted": True, "trashedTo": str(trash)}

        @app.get("/skills/{scope}/{name}/usage")
        async def skill_usage(
            scope: Literal["user", "workspace"],
            name: str,
            workspace: Optional[str] = Query(None),
        ) -> dict[str, object]:
            """S2：使用统计（按 digest 分代）。"""
            package, _source_label, _writable = _skill_package_dir(
                scope, name, workspace
            )
            rows = container.skill_usage_repository.snapshot(scope=scope, name=name)
            return {"items": [row.as_dict() for row in rows]}

        @app.post("/skills/{scope}/{name}/cases/run")
        async def run_skill_cases(
            scope: Literal["user", "workspace"],
            name: str,
            body: SkillCasesRunBody,
            workspace: Optional[str] = Query(None),
        ) -> dict[str, object]:
            """S2：跑技能包声明的用例（对给定 trace/output 或某次真实运行）。"""
            from endless_task.skills import load_package_cases, run_package_case

            package, _source_label, _writable = _skill_package_dir(
                scope, name, workspace
            )
            suite = load_package_cases(package)
            tools_used = list(body.toolsUsed)
            output = body.output
            if body.runId:
                tools_used = [
                    str(record.tool_name)
                    for turn in container.runtime_v2_repository.list_model_turns(
                        body.runId
                    )
                    for record in container.runtime_v2_repository.list_tool_executions(
                        turn.id
                    )
                ]
                run_record = container.runtime_v2_repository.get_run(body.runId)
                entries = container.runtime_v2_repository.list_entries(
                    run_record.lane_id
                )
                from endless_task.runtime_v2.domain import TranscriptEntryType

                output = "\n".join(
                    str(entry.payload.get("content", ""))
                    for entry in entries
                    if entry.type is TranscriptEntryType.ASSISTANT_MESSAGE
                )
            results = [
                run_package_case(case, tools_used=tools_used, output=output)
                for case in suite.cases
            ]
            return {
                "diagnostics": list(suite.diagnostics),
                "cases": [
                    {
                        "name": result.name,
                        "passed": result.passed,
                        "failures": list(result.failures),
                    }
                    for result in results
                ],
                "toolsUsed": tools_used,
            }

        @app.get("/skills/ecosystem/search")
        async def search_ecosystem_skills(
            query: str = Query(min_length=1, max_length=200),
            limit: int = Query(8, ge=1, le=20),
        ) -> dict[str, object]:
            """S8：生态检索（只读，不落盘）。"""
            from endless_task.skills.ecosystem import search_ecosystem

            result = await asyncio.to_thread(
                search_ecosystem, query, limit=limit
            )
            return {
                "query": query,
                "error": result.error,
                "items": [hit.as_dict() for hit in result.hits],
            }

        @app.post("/skills/install", status_code=201)
        async def install_skill(body: SkillInstallBody) -> dict[str, object]:
            """S8：从生态安装技能。

            流程：下载 GitHub tarball 到 staging → **静态扫描门禁** → 原子激活 →
            记录 provenance。``ENDLESS_TASK_SKILL_INSTALL=deny`` 时直接拒绝；
            ``ask``/``allow`` 都允许（UI 点击本身就是用户确认，模型侧工具另走审批）。
            """
            from endless_task.skills import ImportFailure, import_skill_package
            from endless_task.skills.ecosystem import (
                download_skill_package,
                normalize_ecosystem_package,
            )

            if container.settings.skill_install_mode == "deny":
                raise ApiRequestError(
                    "skill_install_disabled",
                    "当前配置禁止从生态安装技能（ENDLESS_TASK_SKILL_INSTALL=deny）。",
                    status_code=403,
                )
            target_root = _skill_root_for(body.scope, body.workspaceId)
            target_root.mkdir(parents=True, exist_ok=True)
            staging = Path(
                tempfile.mkdtemp(
                    prefix="skill-install-",
                    dir=str(container.settings.database_path.parent),
                )
            )
            try:
                source_dir = await asyncio.to_thread(
                    download_skill_package, body.source, staging
                )
                # 生态技能多为 v1（只有 name/description），规整成 v2 后再过扫描门禁。
                normalized, folded_keys = await asyncio.to_thread(
                    normalize_ecosystem_package, source_dir
                )
                try:
                    result = await asyncio.to_thread(
                        import_skill_package,
                        source_dir,
                        target_root,
                        allow_upgrade=body.allowUpgrade,
                    )
                except ImportFailure as failure:
                    raise ApiRequestError(
                        failure.code, failure.message, status_code=400
                    ) from None
            except ValueError as error:
                raise ApiRequestError(
                    "skill_install_failed", str(error), status_code=400
                ) from None
            finally:
                shutil.rmtree(staging, ignore_errors=True)

            worst_level = (
                result.scan_report.worst_level.value
                if result.scan_report is not None
                and result.scan_report.worst_level is not None
                else None
            )
            provenance = container.skill_provenance_repository.record(
                scope=body.scope,
                workspace_id="" if body.scope == "user" else (body.workspaceId or ""),
                name=result.name,
                spec=body.source,
                source=body.source.split("@", 1)[0],
                digest=result.digest,
                worst_level=worst_level,
            )
            return {
                "skill": {
                    "name": result.name,
                    "version": result.version,
                    "digest": result.digest,
                    "target": str(result.target),
                    "upgraded": result.upgraded,
                    "worstLevel": worst_level,
                    "normalized": normalized,
                    "foldedKeys": list(folded_keys),
                },
                "provenance": provenance.as_dict(),
            }

        @app.post("/skills/search")
        async def search_skills(body: SkillSearchBody) -> dict[str, object]:
            """S6：本地技能检索（workspace / global / all）。"""
            root = _workspace_root_path(body.workspaceId)
            hits = container.skill_service.search_skills(
                body.query,
                root,
                workspace_id=body.workspaceId or "",
                scope=body.scope,
                limit=body.limit,
            )
            return {
                "query": body.query,
                "scope": body.scope,
                "items": [
                    {
                        "name": hit.name,
                        "description": hit.description,
                        "scope": hit.scope,
                        "source": hit.source,
                        "score": hit.score,
                    }
                    for hit in hits
                ],
            }

        @app.get("/skills/invocable")
        async def list_invocable_skills(
            workspace_id: Optional[str] = Query(None, alias="workspaceId"),
        ) -> dict[str, object]:
            """S1：composer ``/`` 候选——用户可显式调用、未禁用、清单合法的技能。"""
            root = None
            if workspace_id:
                try:
                    item = container.workspace_repository.get_workspace(workspace_id)
                    root = Path(item.root_path).expanduser() if item.root_path else None
                except Exception:
                    root = None
            skills = container.skill_service.invocable_skills(
                root, workspace_id=workspace_id or ""
            )
            return {
                "items": [
                    {
                        "name": skill.name,
                        "description": skill.description,
                        "scope": skill.scope.value,
                        "whenToUse": skill.when_to_use,
                        "source": skill.source,
                        "pinned": skill.pinned,
                    }
                    for skill in skills
                ]
            }

        @app.patch("/skills/{scope}/{name}")
        async def patch_skill(
            scope: Literal["user", "workspace"],
            name: str,
            body: SkillPatchBody,
            workspace: Optional[str] = Query(None),
        ) -> dict[str, object]:
            from endless_task.skills import SkillScope

            # 用户级技能是全局开关：必须落在 workspace_id="" 上，否则读侧
            # （list_skills 只查 (user, "")）看不到这次禁用——历史 UI 会带工作区参数。
            target_scope = SkillScope(scope)
            workspace_key = "" if target_scope is SkillScope.USER else (workspace or "")
            if body.disabled is None and body.pinned is None:
                raise ApiRequestError(
                    "invalid_request", "需要 disabled 或 pinned 之一。"
                )
            if body.disabled is not None:
                container.skill_service.set_disabled(
                    scope=target_scope,
                    name=name,
                    disabled=body.disabled,
                    workspace_id=workspace_key,
                )
            if body.pinned is not None:
                container.skill_service.set_pinned(
                    scope=target_scope,
                    name=name,
                    pinned=body.pinned,
                    workspace_id=workspace_key,
                )
            return {"disabled": body.disabled, "pinned": body.pinned}
