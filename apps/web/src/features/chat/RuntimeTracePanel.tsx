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

// 将后端 errorCode 映射为对人类/模型都友好的说明，避免直接把内部编码暴露给用户。
const errorLabel: Record<string, string> = {
  invalid_tool_arguments: "工具参数无效",
  invalid_tool_result: "工具返回结果无效",
  tool_not_found: "工具不存在",
  invalid_tool_name: "工具名无效",
  cancelled: "工具已取消",
  timeout: "工具执行超时",
};

const describeError = (tool: RuntimeToolItem): string => {
  if (!tool.errorCode) return "";
  const base = errorLabel[tool.errorCode] ?? tool.errorCode;
  // result 里通常携带反馈给模型的 safe_message，优先展示它。
  const detail = tool.result?.trim();
  return detail ? `${base}：${detail}` : base;
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

// 从 structuredContent 里识别 terminal 结果（run_shell 等），渲染成退出码 + 输出。
const readTerminal = (
  structured: unknown,
): { exitCode: number | null; output: string } | null => {
  if (!structured || typeof structured !== "object") return null;
  const record = structured as Record<string, unknown>;
  const exitCode =
    typeof record["exitCode"] === "number" ? record["exitCode"] : null;
  const stdout = typeof record["stdout"] === "string" ? record["stdout"] : "";
  const stderr = typeof record["stderr"] === "string" ? record["stderr"] : "";
  if (exitCode === null && !stdout && !stderr) return null;
  const output = [stdout, stderr ? `[stderr]\n${stderr}` : ""]
    .filter(Boolean)
    .join("\n");
  return { exitCode, output };
};

export function RuntimeToolCard({ tool }: { tool: RuntimeToolItem }) {
  const [open, setOpen] = useState(false);
  const hasBody = tool.hasArgs || tool.hasResult || tool.isError;
  const errorText = describeError(tool);

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
        <span className={`runtime-tool-status is-${tool.phase}`}>
          {phaseLabel[tool.phase] ?? tool.phase}
        </span>
        {tool.isError ? (
          <span className="runtime-tool-error" title={errorText}>
            {errorText || "执行出错"}
          </span>
        ) : null}
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
          {hasBody ? (
            <div className="runtime-tool-section">
              <span className="runtime-tool-label">结果</span>
              {errorText ? (
                <p className="runtime-tool-error runtime-tool-error-block">
                  {errorText}
                </p>
              ) : null}
              {renderResult(tool)}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function renderResult(tool: RuntimeToolItem) {
  const terminal = readTerminal(tool.structuredContent);
  if (terminal && (terminal.output || terminal.exitCode !== null)) {
    return (
      <div className="runtime-tool-terminal">
        {terminal.exitCode !== null ? (
          <span
            className={`runtime-tool-exit is-${terminal.exitCode === 0 ? "ok" : "err"}`}
          >
            退出码 {terminal.exitCode}
          </span>
        ) : null}
        {terminal.output ? (
          <pre className="runtime-tool-pre runtime-tool-terminal-pre">
            {terminal.output}
          </pre>
        ) : null}
      </div>
    );
  }
  if (tool.hasResult || tool.structuredContent !== undefined) {
    return (
      <pre className="runtime-tool-pre">{formatValue(tool.result || tool.structuredContent)}</pre>
    );
  }
  return null;
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
