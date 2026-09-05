import { useCallback, useEffect, useState } from "react";
import { EmptyState } from "../ui/EmptyState";
import { chatApi } from "../chat/api";
import type { Skill, SkillPackagesResponse } from "../chat/apiTypes";

type SkillsContentProps = {
  onChanged?: () => void | Promise<void>;
  workspaceId: string | null;
};

export function SkillsContent({ onChanged, workspaceId }: SkillsContentProps) {
  const [skills, setSkills] = useState<Skill[]>([]);
  const [userDirectory, setUserDirectory] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busyName, setBusyName] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [packages, setPackages] = useState<SkillPackagesResponse | null>(null);

  useEffect(() => {
    void chatApi
      .listSkillPackages(workspaceId)
      .then(setPackages)
      .catch(() => setPackages(null));
  }, [workspaceId]);

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

  useEffect(() => {
    void load();
  }, [load]);

  const toggleDisabled = async (skill: Skill) => {
    setBusyName(skill.name);
    setActionError(null);
    try {
      await chatApi.patchSkill(
        skill.scope,
        skill.name,
        !skill.disabled,
        workspaceId,
      );
      await load();
      await onChanged?.();
    } catch {
      setActionError("更新技能状态失败，请重试。");
    } finally {
      setBusyName(null);
    }
  };

  return (
    <div className="panel-content">
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
      <div className="knowledge-toolbar">
        <p className="skill-directory">{userDirectory}</p>
        <button onClick={() => void load()} type="button">
          刷新
        </button>
      </div>
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
          desc="把技能文件夹放入上方目录。每个技能是一个包含 SKILL.md 的文件夹，助手会在任务匹配时自动读取。"
          title="还没有技能"
        />
      ) : null}
      {skills.map((skill) => (
        <div
          className="memory-item knowledge-item"
          key={`${skill.scope}:${skill.name}`}
        >
          <p className="memory-content knowledge-title">{skill.name}</p>
          <p className="memory-content">{skill.description}</p>
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
            {skill.disableModelInvocation ? (
              <span className="knowledge-badge">仅手动</span>
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
                ? "更新中…"
                : skill.disabled
                  ? "启用"
                  : "禁用"}
            </button>
            <button
              onClick={() => void chatApi.revealInFinder(skill.filePath)}
              type="button"
            >
              在访达中显示
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
