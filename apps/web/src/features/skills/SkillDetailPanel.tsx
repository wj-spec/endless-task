import { useState } from "react";
import { chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import type { Skill, SkillCaseResult, SkillUsageRow } from "../chat/apiTypes";

export type SkillDetailPanelProps = {
  skill: Skill;
  workspaceId: string;
  onChanged: () => void;
};

const KIND_LABELS: Record<string, string> = {
  surfaced: "进入目录",
  invoked: "被调用",
  body_read: "读正文",
  missing_dependencies: "依赖不满足",
};

const formatTime = (value: string | null) =>
  value ? new Date(value).toLocaleString() : "—";

/**
 * S2 技能卡片详情：使用统计 + 用例试跑（可对某次真实运行校验）。
 */
export function SkillDetailPanel({
  skill,
  workspaceId,
  onChanged,
}: SkillDetailPanelProps) {
  const [usage, setUsage] = useState<SkillUsageRow[] | null>(null);
  const [cases, setCases] = useState<SkillCaseResult[] | null>(null);
  const [diagnostics, setDiagnostics] = useState<string[]>([]);
  const [runId, setRunId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadUsage = async () => {
    setBusy(true);
    setError(null);
    try {
      setUsage(
        await chatApi.skillUsage(skill.scope, skill.name, workspaceId || null),
      );
    } catch (cause: unknown) {
      setError(readableError(cause) || "读取使用记录失败。");
    } finally {
      setBusy(false);
    }
  };

  const runCases = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await chatApi.runSkillCases(
        skill.scope,
        skill.name,
        runId.trim() ? { runId: runId.trim() } : {},
        workspaceId || null,
      );
      setCases(result.cases);
      setDiagnostics(result.diagnostics);
    } catch (cause: unknown) {
      setError(readableError(cause) || "运行用例失败。");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="skill-detail">
      <div className="skill-form-row">
        <button disabled={busy} onClick={() => void loadUsage()} type="button">
          使用记录
        </button>
        <input
          aria-label="运行 ID"
          onChange={(event) => setRunId(event.target.value)}
          placeholder="可选：某次 runId（留空用手工 trace）"
          value={runId}
        />
        <button disabled={busy} onClick={() => void runCases()} type="button">
          试跑用例
        </button>
        <button
          onClick={() => void chatApi.revealInFinder(skill.filePath)}
          type="button"
        >
          在访达中显示
        </button>
        <button onClick={onChanged} type="button">
          刷新列表
        </button>
      </div>
      {error ? (
        <p className="proposal-error" role="alert">
          {error}
        </p>
      ) : null}
      {usage ? (
        usage.length === 0 ? (
          <p className="file-tree-note">还没有使用记录。</p>
        ) : (
          <ul className="skill-usage-list">
            {usage.map((row) => (
              <li key={row.digest}>
                <code>{row.digest.slice(0, 8)}</code>
                {Object.entries(row.counts).map(([kind, count]) => (
                  <span className="knowledge-badge" key={kind}>
                    {KIND_LABELS[kind] ?? kind} {count}
                  </span>
                ))}
                <span className="skill-usage-time">最近 {formatTime(row.lastAt)}</span>
              </li>
            ))}
          </ul>
        )
      ) : null}
      {diagnostics.length > 0 ? (
        <ul className="skill-case-list">
          {diagnostics.map((item) => (
            <li className="is-failed" key={item}>
              {item}
            </li>
          ))}
        </ul>
      ) : null}
      {cases ? (
        cases.length === 0 ? (
          <p className="file-tree-note">该技能包没有声明用例（tests/cases.yaml）。</p>
        ) : (
          <ul className="skill-case-list">
            {cases.map((item) => (
              <li className={item.passed ? "is-passed" : "is-failed"} key={item.name}>
                {item.passed ? "✓" : "✗"} {item.name}
                {item.failures.length > 0 ? ` — ${item.failures.join("；")}` : ""}
              </li>
            ))}
          </ul>
        )
      ) : null}
    </div>
  );
}
