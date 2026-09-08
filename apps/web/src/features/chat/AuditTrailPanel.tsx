import { useEffect, useState } from "react";
import { chatApi } from "./api";
import type { AuditTrailEntry } from "./apiTypes";
import {
  auditKindLabel,
  auditOptionLabel,
  auditSummaryLine,
  hasWhy,
  orderAuditEntries,
  severityLabel,
} from "./auditTrail";

type AuditTrailPanelProps = {
  runId: string | null;
  /** 运行结束时变化，用来触发重新拉取。 */
  refreshKey?: string | number | null;
};

/**
 * A8 决策解释 / 审计轨迹：一次运行的"做了什么 + 为什么"。
 *
 * 理由来自运行事实（效果/审批/错误码/升级原因/验证结论），不是模型自述；
 * 默认折叠，按需展开，避免信息淹没。
 */
export const AuditTrailPanel = ({
  runId,
  refreshKey = null,
}: AuditTrailPanelProps) => {
  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<AuditTrailEntry[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!open || !runId) return;
    let cancelled = false;
    setLoading(true);
    const load = async () => {
      try {
        const result = await chatApi.getRunAuditTrail(runId);
        if (cancelled) return;
        setEntries(orderAuditEntries(result.items));
        setCounts(result.counts);
        setError(null);
      } catch {
        if (!cancelled) setError("无法读取审计轨迹，请重试。");
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [open, runId, refreshKey]);

  if (!runId) return null;
  return (
    <section className="audit-trail">
      <button
        aria-expanded={open}
        className="audit-trail-toggle"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        {open ? "收起审计轨迹" : "审计轨迹（为什么这么做）"}
        {open && entries.length > 0 ? (
          <span className="audit-trail-count">
            {auditSummaryLine(counts)}
          </span>
        ) : null}
      </button>
      {open ? (
        <div className="audit-trail-body">
          {loading ? <p className="audit-trail-hint">读取中…</p> : null}
          {error ? (
            <p className="audit-trail-error" role="alert">
              {error}
            </p>
          ) : null}
          {!loading && !error && entries.length === 0 ? (
            <p className="audit-trail-hint">这次运行还没有可展示的决策记录。</p>
          ) : null}
          {entries.map((entry) => (
            <article
              className={`audit-entry audit-${entry.severity}`}
              key={entry.id}
            >
              <header className="audit-entry-head">
                <span className="audit-entry-kind">
                  {auditKindLabel(entry.kind)}
                </span>
                <span className="audit-entry-title">{entry.title}</span>
                <span className="audit-entry-severity">
                  {severityLabel(entry.severity)}
                </span>
                {hasWhy(entry) ? (
                  <button
                    className="audit-entry-why"
                    onClick={() =>
                      setExpandedId(expandedId === entry.id ? null : entry.id)
                    }
                    type="button"
                  >
                    {expandedId === entry.id ? "收起" : "为什么？"}
                  </button>
                ) : null}
              </header>
              {entry.summary ? (
                <p className="audit-entry-summary">{entry.summary}</p>
              ) : null}
              {expandedId === entry.id ? (
                <div className="audit-entry-why-body">
                  {entry.rationale ? (
                    <p>
                      <strong>理由：</strong>
                      {entry.rationale}
                    </p>
                  ) : null}
                  {entry.counterfactual ? (
                    <p>
                      <strong>如果不这样：</strong>
                      {entry.counterfactual}
                    </p>
                  ) : null}
                  {entry.uncertainty ? (
                    <p>
                      <strong>不确定性：</strong>
                      {entry.uncertainty}
                    </p>
                  ) : null}
                  {entry.options.length > 0 ? (
                    <p>
                      <strong>可选项：</strong>
                      {entry.options.map(auditOptionLabel).join(" / ")}
                    </p>
                  ) : null}
                </div>
              ) : null}
            </article>
          ))}
        </div>
      ) : null}
    </section>
  );
};
