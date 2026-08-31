import { useState } from "react";
import { ChevronIcon } from "../ui/Icons";
import type { RuntimeToolItem, RuntimeToolPhase } from "./runtimeTrace";

const phaseLabel: Record<RuntimeToolPhase, string> = {
  pending: "等待",
  running: "执行中",
  waiting: "待确认",
  completed: "完成",
  failed: "失败",
  rejected: "已拒绝",
  cancelled: "已取消",
};

const formatValue = (value: unknown): string => {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

function RuntimeToolCard({ tool }: { tool: RuntimeToolItem }) {
  const [open, setOpen] = useState(false);
  const hasBody = tool.hasArgs || tool.hasResult || Boolean(tool.errorCode);

  return (
    <div className={`runtime-tool-card is-${tool.phase}`}>
      <button
        aria-expanded={hasBody ? open : undefined}
        className="runtime-tool-head"
        disabled={!hasBody}
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span aria-hidden="true" className="runtime-tool-dot" />
        <span className="runtime-tool-name">{tool.toolName}</span>
        <span className="runtime-tool-status">
          {phaseLabel[tool.phase] ?? tool.phase}
        </span>
        {hasBody ? (
          <span className="runtime-tool-chevron">
            <ChevronIcon direction={open ? "up" : "down"} size={14} />
          </span>
        ) : null}
      </button>

      {open && hasBody ? (
        <div className="runtime-tool-body">
          {tool.hasArgs ? (
            <div className="runtime-tool-section">
              <span className="runtime-tool-label">参数</span>
              <pre className="runtime-tool-pre">{formatValue(tool.arguments)}</pre>
            </div>
          ) : null}
          {tool.hasResult || tool.errorCode ? (
            <div className="runtime-tool-section">
              <span className="runtime-tool-label">结果</span>
              {tool.errorCode ? (
                <p className="runtime-tool-error">{tool.errorCode}</p>
              ) : null}
              <pre className="runtime-tool-pre">{formatValue(tool.result)}</pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Granular per-tool execution trace for the active run (pi-style). It renders
 * one expandable card per executed tool with its arguments + result. It is the
 * *visual* layer only — the top-level run status live region stays on the turn
 * badge so this list never competes as a second live region.
 */
export function RuntimeTracePanel({ tools }: { tools: RuntimeToolItem[] }) {
  if (!tools.length) return null;
  return (
    <div className="runtime-trace" aria-label="工具执行过程">
      {tools.map((tool) => (
        <RuntimeToolCard key={tool.key} tool={tool} />
      ))}
    </div>
  );
}