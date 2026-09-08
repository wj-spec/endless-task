import { useState } from "react";

export type PlanStep = { title?: unknown; status?: unknown };
export type PlanPayload = {
  title?: unknown;
  steps?: PlanStep[];
  currentStepIndex?: unknown;
};

const STEP_LABELS: Record<string, string> = {
  pending: "待办",
  in_progress: "进行中",
  completed: "完成",
  skipped: "跳过",
  failed: "失败",
};

const stepLabel = (status: unknown): string => {
  const key = typeof status === "string" ? status : "";
  return STEP_LABELS[key] ?? "—";
};

const stepText = (step: PlanStep): string =>
  typeof step.title === "string" ? step.title : "（未命名步骤）";

const stepStatus = (step: PlanStep, index: number, derivedIndex: number | null): string => {
  if (typeof step.status === "string" && step.status) return step.status;
  if (derivedIndex === null) return "pending";
  if (index < derivedIndex) return "completed";
  if (index === derivedIndex) return "in_progress";
  return "pending";
};

const STATUS_MARKER: Record<string, string> = {
  completed: "✓",
  in_progress: "●",
  pending: "○",
  skipped: "⤳",
  failed: "✗",
};

const deriveCurrentIndex = (plan: PlanPayload): number | null => {
  if (typeof plan.currentStepIndex === "number" && plan.currentStepIndex >= 0) {
    return plan.currentStepIndex;
  }
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const idx = steps.findIndex((step) => step.status === "in_progress");
  return idx >= 0 ? idx : null;
};

type PlanLineProps = {
  plan: PlanPayload;
  /** 运行中：默认展开并给“进行中”摘要；终态默认折叠为摘要行（D3）。 */
  active?: boolean;
  busy?: boolean;
};

/** 轮内可折叠计划行（S-P1-3a）：显示计划标题、进度摘要与步骤（展开）。 */
export function PlanLine({ plan, active = false, busy = false }: PlanLineProps) {
  const [expanded, setExpanded] = useState(active);
  const steps = Array.isArray(plan.steps) ? plan.steps : [];
  const derivedIndex = deriveCurrentIndex(plan);
  const completed = steps.filter((step) => step.status === "completed").length;
  const remaining = steps.filter((step, index) => {
    if (step.status === "completed" || step.status === "skipped") return false;
    if (derivedIndex !== null && index < derivedIndex) return false;
    return true;
  }).length;
  const title = typeof plan.title === "string" ? plan.title : "计划";

  const summary = busy
    ? "运行中"
    : active
      ? `进行中 ${completed}/${steps.length}`
      : steps.length > 0
        ? remaining > 0
          ? `剩余 ${remaining} 步`
          : "已完成"
        : "已记录 0 步";

  return (
    <section aria-label="计划" className={`plan-line${active ? " is-active" : ""}`}>
      <div className="plan-line-head">
        <span className="plan-line-title">{title}</span>
        <span className="plan-line-summary">{summary}</span>
        {steps.length > 0 ? (
          <button
            aria-expanded={expanded}
            aria-label={expanded ? "收起计划" : "展开计划"}
            className="plan-line-toggle"
            disabled={busy}
            onClick={() => setExpanded((current) => !current)}
            type="button"
          >
            {expanded ? "收起" : "展开"}
          </button>
        ) : null}
      </div>
      {expanded && steps.length > 0 ? (
        <ol className="plan-line-steps">
          {steps.map((step, index) => {
            const status = stepStatus(step, index, derivedIndex);
            return (
              <li className={`is-${status}`} key={`${index}-${stepText(step)}`}>
                <span aria-hidden="true" className="plan-line-step-marker">
                  {STATUS_MARKER[status] ?? "○"}
                </span>
                <span className="plan-line-step-title">{stepText(step)}</span>
                <span className="plan-line-step-status">{stepLabel(status)}</span>
              </li>
            );
          })}
        </ol>
      ) : null}
    </section>
  );
}
