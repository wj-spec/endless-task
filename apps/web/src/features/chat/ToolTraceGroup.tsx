import { useState } from "react";
import type { RuntimeToolItem } from "./runtimeTrace";
import { RuntimeToolCard } from "./RuntimeTracePanel";
import { ChevronIcon } from "../ui/Icons";

type ToolTraceGroupProps = {
  tools: RuntimeToolItem[];
  /** 该 run 是否仍在运行（用于摘要文案）。 */
  active?: boolean;
};

const phaseSummary = (
  tools: RuntimeToolItem[],
  active: boolean,
): { label: string; tone: "ok" | "warn" | "danger" } => {
  const failed = tools.filter((tool) => tool.isError).length;
  const pending = tools.filter(
    (tool) => tool.phase === "running" || tool.phase === "waiting" || tool.phase === "pending",
  ).length;
  if (failed > 0) return { label: `${failed} 个失败`, tone: "danger" };
  if (pending > 0 || active) return { label: "执行中", tone: "warn" };
  return { label: "全部完成", tone: "ok" };
};

/**
 * 工具执行轨迹分组：**默认折叠**为一行摘要（`工具执行 · N 步 · 状态`），
 * 展开后才逐条显示工具卡。回答正文不在这里，所以折叠不会隐藏答案。
 */
export function ToolTraceGroup({ tools, active = false }: ToolTraceGroupProps) {
  const [open, setOpen] = useState(false);
  if (!tools.length) return null;
  const summary = phaseSummary(tools, active);
  return (
    <div className="tool-trace-group">
      <button
        aria-expanded={open}
        className={`tool-trace-head is-${summary.tone}`}
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span className="tool-trace-label">工具执行</span>
        <span className="tool-trace-count">{tools.length} 步</span>
        <span className={`tool-trace-status is-${summary.tone}`}>
          {summary.label}
        </span>
        <span className="tool-trace-chevron">
          <ChevronIcon direction={open ? "up" : "down"} size={14} />
        </span>
      </button>
      {open ? (
        <div className="tool-trace-body" aria-label="工具执行过程">
          {tools.map((tool) => (
            <RuntimeToolCard key={tool.key} tool={tool} />
          ))}
        </div>
      ) : null}
    </div>
  );
}
