import { useState } from "react";
import {
  BookIcon,
  ChevronIcon,
  FileIcon,
  FolderIcon,
  GlobeIcon,
  SparkleIcon,
  TerminalIcon,
  WrenchIcon,
} from "../ui/Icons";
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

// 按工具名归类到一个可辨识的图标，便于快速扫视（第 27 章：工具图标与品牌）。
const toolIcon = (toolName: string) => {
  const name = toolName.toLowerCase();
  if (/(shell|bash|terminal|exec|command|python|run_)/.test(name)) {
    return TerminalIcon;
  }
  if (/(search|query|web|fetch|browse|http)/.test(name)) {
    return GlobeIcon;
  }
  if (/(list|read|write|append|delete|mkdir|file|workspace_)/.test(name)) {
    return name.includes("list") ? FolderIcon : FileIcon;
  }
  if (/(memory|remember|recall|knowledge|source)/.test(name)) {
    return BookIcon;
  }
  if (/(plan|artifact|task|reminder|summar)/.test(name)) {
    return SparkleIcon;
  }
  return WrenchIcon;
};

// 生成一行可读的工具输入摘要（避免把大 JSON 平铺在卡片头部）。
const inputSummary = (tool: RuntimeToolItem): string => {
  const args = tool.arguments as Record<string, unknown> | null | undefined;
  if (!args || typeof args !== "object") return "";
  const val = (key: string) => {
    const item = args[key];
    return typeof item === "string" || typeof item === "number"
      ? String(item)
      : "";
  };
  const label =
    val("path") ||
    val("query") ||
    val("cmd") ||
    val("command") ||
    val("name") ||
    val("file") ||
    val("url") ||
    val("folder") ||
    val("clause") ||
    "";
  if (label) return label;
  // 兜底：把原始参数压缩成一行。
  try {
    return JSON.stringify(args).replace(/\s+/g, " ");
  } catch {
    return "";
  }
};

export function RuntimeToolCard({ tool }: { tool: RuntimeToolItem }) {
  const [open, setOpen] = useState(false);
  const hasBody = tool.hasArgs || tool.hasResult || tool.isError;
  const errorText = describeError(tool);
  const Icon = toolIcon(tool.toolName);
  const summary = inputSummary(tool);

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
        <span aria-hidden="true" className="runtime-tool-icon">
          <Icon size={15} />
        </span>
        <span className="runtime-tool-name">{tool.toolName}</span>
        {summary ? (
          <span className="runtime-tool-summary" title={summary}>
            {summary}
          </span>
        ) : null}
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
  const [open, setOpen] = useState(false);
  if (!tools.length) return null;
  // 回退路径（没有交错时间线时）：与时间线一致，默认折叠为一行摘要。
  const failed = tools.filter((tool) => tool.isError).length;
  const tone = failed > 0 ? "danger" : "ok";
  return (
    <div className="runtime-trace">
      <button
        aria-expanded={open}
        className={`tool-trace-head is-${tone}`}
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span className="tool-trace-label">工具执行</span>
        <span className="tool-trace-count">{tools.length} 步</span>
        <span className={`tool-trace-status is-${tone}`}>
          {failed > 0 ? `${failed} 个失败` : "全部完成"}
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
