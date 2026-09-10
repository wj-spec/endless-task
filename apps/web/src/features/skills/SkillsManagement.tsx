import { useCallback, useEffect, useState } from "react";
import { EmptyState } from "../ui/EmptyState";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import type { Skill, SkillPackagesResponse } from "../chat/apiTypes";
import { SkillCreateForm } from "./SkillCreateForm";
import { SkillDetailPanel } from "./SkillDetailPanel";
import { SkillImportForm } from "./SkillImportForm";

type SkillsContentProps = {
  onChanged?: () => void | Promise<void>;
  workspaceId: string | null;
  /**
   * S7：把技能注入当前会话的下一轮（等价于在输入框预置 `/技能名 `）。
   * 由 App 提供，实现"用户指定 → 直接注入"这条通道。
   */
  onInjectSkill?: (name: string) => void;
};

type FormMode = "none" | "import" | "create";

export function SkillsContent({
  onChanged,
  workspaceId,
  onInjectSkill,
}: SkillsContentProps) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [userDirectory, setUserDirectory] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyName, setBusyName] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [packages, setPackages] = useState<SkillPackagesResponse | null>(null);
  const [form, setForm] = useState<FormMode>("none");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState<Skill | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const response = await chatApi.listSkills(workspaceId);
      setSkills(response.items);
      setUserDirectory(response.userSkillsDirectory);
      setLoadError(null);
    } catch {
      setLoadError("无法加载技能，请重试。");
    } finally {
      setLoading(false);
    }
  }, [workspaceId]);

  const loadPackages = useCallback(() => {
    void chatApi
      .listSkillPackages(workspaceId)
      .then(setPackages)
      .catch(() => setPackages(null));
  }, [workspaceId]);

  useEffect(() => {
    void load();
    loadPackages();
  }, [load, loadPackages]);

  const refreshAll = useCallback(async () => {
    await load();
    loadPackages();
    await onChanged?.();
  }, [load, loadPackages, onChanged]);

  const toggleDisabled = async (skill: Skill) => {
    setBusyName(skill.name);
    setActionError(null);
    try {
      await chatApi.patchSkill(
        skill.scope,
        skill.name,
        { disabled: !skill.disabled },
        workspaceId,
      );
      await refreshAll();
    } catch {
      setActionError("更新技能状态失败，请重试。");
    } finally {
      setBusyName(null);
    }
  };

  const togglePinned = async (skill: Skill) => {
    setBusyName(skill.name);
    setActionError(null);
    try {
      await chatApi.patchSkill(
        skill.scope,
        skill.name,
        { pinned: !skill.pinned },
        workspaceId,
      );
      await refreshAll();
    } catch {
      setActionError("更新固定状态失败，请重试。");
    } finally {
      setBusyName(null);
    }
  };

  const remove = async (skill: Skill) => {
    setBusyName(skill.name);
    setActionError(null);
    try {
      await chatApi.deleteSkill(skill.scope, skill.name, workspaceId);
      await refreshAll();
    } catch (cause: unknown) {
      setActionError(readableError(cause) || "删除失败。");
    } finally {
      setBusyName(null);
      setConfirmRemove(null);
    }
  };

  const packageOf = (skill: Skill) =>
    packages?.packages.find(
      (item) => item.scope === skill.scope && item.name === skill.name,
    );

  return (
    <div className="panel-content">
      <div className="knowledge-toolbar">
        <p className="skill-directory">{userDirectory || "技能目录"}</p>
        <button onClick={() => void refreshAll()} type="button">
          刷新
        </button>
        <button
          onClick={() => setForm(form === "import" ? "none" : "import")}
          type="button"
        >
          导入技能
        </button>
        <button
          onClick={() => setForm(form === "create" ? "none" : "create")}
          type="button"
        >
          新建技能
        </button>
      </div>

      {form === "import" ? (
        <SkillImportForm
          onCancel={() => setForm("none")}
          onDone={() => void refreshAll()}
          workspaceId={workspaceId ?? ""}
        />
      ) : null}
      {form === "create" ? (
        <SkillCreateForm
          onCancel={() => setForm("none")}
          onDone={() => void refreshAll()}
          workspaceId={workspaceId ?? ""}
        />
      ) : null}

      {packages?.enabled ? (
        <section aria-label="技能包" className="skill-packages-note">
          <strong>技能包（v2 registry）</strong>
          {packages.packages.length === 0 ? (
            <p>已开启但当前根目录没有可用技能包。</p>
          ) : (
            <ul>
              {packages.packages.map((skill) => (
                <li key={`${skill.scope}/${skill.name}`}>
                  {skill.name}@{skill.version}
                  {skill.invocable ? " · 可用" : ` · ${skill.state}`}
                </li>
              ))}
            </ul>
          )}
          {packages.conflicts.length > 0 ? (
            <p className="skill-packages-conflicts">
              冲突 {packages.conflicts.length} 项（registry 已拒绝歧义版本）
            </p>
          ) : null}
        </section>
      ) : null}

      {actionError ? (
        <div className="proposal-error" role="alert">
          {actionError}
        </div>
      ) : null}
      {loading ? (
        <div aria-hidden="true" className="skeleton-panel">
          <span className="skeleton-line" />
          <span className="skeleton-line is-short" />
          <span className="skeleton-line" />
        </div>
      ) : null}
      {!loading && loadError ? (
        <EmptyState
          action={
            <button onClick={() => void load()} type="button">
              重试
            </button>
          }
          desc={loadError}
          title="没加载出来"
        />
      ) : null}
      {!loading && !loadError && skills.length === 0 ? (
        <EmptyState
          desc="点上方「新建技能」按模板创建，或「导入技能」把已有目录装进来。"
          title="还没有技能"
        />
      ) : null}
      {skills.map((skill) => {
        const key = `${skill.scope}:${skill.name}`;
        const pack = packageOf(skill);
        return (
          <div className="memory-item knowledge-item" key={key}>
            <p className="memory-content knowledge-title">{skill.name}</p>
            <p className="memory-content">{skill.description}</p>
            {skill.whenToUse ? (
              <p className="memory-content skill-when">适用：{skill.whenToUse}</p>
            ) : null}
            <div className="memory-meta">
              <span
                className={
                  skill.scope === "user"
                    ? "knowledge-badge is-global"
                    : "knowledge-badge is-workspace"
                }
              >
                {skill.scope === "user" ? "用户级" : "工作区"}
              </span>
              {skill.source ? (
                <span className="knowledge-badge">{skill.source}</span>
              ) : null}
              {skill.pinned ? (
                <span className="knowledge-badge is-pinned">已固定</span>
              ) : null}
              {skill.inCatalog === false ? (
                <span
                  className="knowledge-badge"
                  title="不在模型的默认目录里；可用 /技能名 显式调用，模型也能通过 skill_search 检索到"
                >
                  未进目录
                </span>
              ) : null}
              {pack ? (
                <span className="knowledge-badge">v{pack.version}</span>
              ) : skill.version ? (
                <span className="knowledge-badge">v{skill.version}</span>
              ) : null}
              {pack?.quarantined ? (
                <span className="memory-status is-expired">已隔离</span>
              ) : null}
              {skill.disableModelInvocation ? (
                <span className="knowledge-badge">仅手动</span>
              ) : null}
              {skill.disabled ? (
                <span className="memory-status is-expired">已禁用</span>
              ) : null}
              {skill.diagnostics.length > 0 ? (
                <span className="memory-status is-expired">
                  {skill.diagnostics[0].message}
                </span>
              ) : null}
            </div>
            <code className="skill-path">{skill.filePath}</code>
            <div className="memory-actions">
              <button
                disabled={busyName === skill.name}
                onClick={() => void toggleDisabled(skill)}
                type="button"
              >
                {busyName === skill.name
                  ? "处理中…"
                  : skill.disabled
                    ? "启用"
                    : "禁用"}
              </button>
              {onInjectSkill ? (
                <button
                  onClick={() => onInjectSkill(skill.name)}
                  title="在输入框预置 /技能名，发送时直接注入该技能正文"
                  type="button"
                >
                  注入到对话
                </button>
              ) : null}
              <button
                disabled={busyName === skill.name}
                onClick={() => void togglePinned(skill)}
                title="固定后进入模型的默认技能目录"
                type="button"
              >
                {skill.pinned ? "取消固定" : "固定到目录"}
              </button>
              <button
                aria-expanded={expanded === key}
                onClick={() => setExpanded(expanded === key ? null : key)}
                type="button"
              >
                {expanded === key ? "收起详情" : "详情 / 用例"}
              </button>
              <button
                onClick={() => void chatApi.revealInFinder(skill.filePath)}
                type="button"
              >
                在访达中显示
              </button>
              <button
                className="danger-action"
                disabled={busyName === skill.name}
                onClick={() => setConfirmRemove(skill)}
                type="button"
              >
                删除
              </button>
            </div>
            {expanded === key ? (
              <SkillDetailPanel
                onChanged={() => void refreshAll()}
                skill={skill}
                workspaceId={workspaceId ?? ""}
              />
            ) : null}
          </div>
        );
      })}

      {confirmRemove ? (
        <ConfirmDialog
          body={`将把「${confirmRemove.name}」移入技能目录下的 .trash（可手工找回），并立即从模型可见目录中移除。`}
          confirmLabel="删除技能"
          onClose={() => setConfirmRemove(null)}
          onConfirm={() => void remove(confirmRemove)}
          title="删除这个技能？"
          tone="danger"
        />
      ) : null}
    </div>
  );
}
