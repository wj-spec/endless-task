import { useCallback, useEffect, useState } from "react";
import { EmptyState } from "../ui/EmptyState";
import { chatApi } from "../chat/api";
import type { McpServer } from "../chat/apiTypes";

type McpContentProps = object;

type McpForm = {
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string;
  url: string;
  env: string;
  headers: string;
  toolCallTimeoutSeconds: string;
};

const emptyForm: McpForm = {
  name: "",
  transport: "stdio",
  command: "",
  args: "",
  url: "",
  env: "{}",
  headers: "{}",
  toolCallTimeoutSeconds: "60",
};

function parseMapping(value: string): Record<string, string> {
  if (!value.trim()) return {};
  const parsed: unknown = JSON.parse(value);
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    Array.isArray(parsed) ||
    Object.entries(parsed).some(([, item]) => typeof item !== "string")
  ) {
    throw new Error("JSON 必须是字符串到字符串的对象");
  }
  return parsed as Record<string, string>;
}

export function McpContent(_: McpContentProps) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState<McpForm>(emptyForm);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setServers(await chatApi.listMcpServers());
      setLoadError(null);
    } catch {
      setLoadError("无法加载 MCP 服务器，请重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const updateForm = (patch: Partial<McpForm>) => {
    setForm((current) => ({ ...current, ...patch }));
  };

  const createServer = async () => {
    setActionError(null);
    try {
      const env = parseMapping(form.env);
      const headers = parseMapping(form.headers);
      await chatApi.createMcpServer({
        name: form.name.trim(),
        transport: form.transport,
        command: form.command.trim(),
        args: form.args
          .split(/\s+/)
          .map((item) => item.trim())
          .filter(Boolean),
        env,
        headers,
        url: form.url.trim(),
        toolCallTimeoutSeconds: Number(form.toolCallTimeoutSeconds || "60"),
      });
      setForm(emptyForm);
      setAdding(false);
      await load();
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : "添加 MCP 服务器失败，请重试。",
      );
    }
  };

  const toggleEnabled = async (server: McpServer) => {
    setBusyId(server.id);
    setActionError(null);
    try {
      await chatApi.patchMcpServer(server.id, { enabled: !server.enabled });
      await load();
    } catch {
      setActionError("更新 MCP 服务器失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  const reloadServer = async (server: McpServer) => {
    setBusyId(server.id);
    setActionError(null);
    try {
      await chatApi.reloadMcpServer(server.id);
      await load();
    } catch {
      setActionError("重连 MCP 服务器失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  const removeServer = async (server: McpServer) => {
    if (!window.confirm(`删除 MCP 服务器「${server.name}」？`)) return;
    setBusyId(server.id);
    setActionError(null);
    try {
      await chatApi.deleteMcpServer(server.id);
      await load();
    } catch {
      setActionError("删除 MCP 服务器失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="panel-content">
      <div className="knowledge-toolbar">
        <p className="mcp-summary">
          MCP 工具会进入助手工具列表；外部动作仍走审批，危险工具始终确认。
        </p>
        <button onClick={() => setAdding((value) => !value)} type="button">
          {adding ? "取消" : "添加服务器"}
        </button>
      </div>
      {actionError ? (
        <div className="proposal-error" role="alert">
          {actionError}
        </div>
      ) : null}
      {adding ? (
        <div className="knowledge-form mcp-form">
          <input
            aria-label="服务器名称"
            onChange={(event) => updateForm({ name: event.target.value })}
            placeholder="名称（小写字母、数字、-、_）"
            value={form.name}
          />
          <select
            aria-label="传输类型"
            onChange={(event) =>
              updateForm({
                transport: event.target.value as "stdio" | "http",
              })
            }
            value={form.transport}
          >
            <option value="stdio">stdio</option>
            <option value="http">HTTP</option>
          </select>
          {form.transport === "stdio" ? (
            <>
              <input
                aria-label="启动命令"
                onChange={(event) => updateForm({ command: event.target.value })}
                placeholder="命令，例如 npx"
                value={form.command}
              />
              <input
                aria-label="命令参数"
                onChange={(event) => updateForm({ args: event.target.value })}
                placeholder="参数（空格分隔）"
                value={form.args}
              />
              <textarea
                aria-label="环境变量 JSON"
                onChange={(event) => updateForm({ env: event.target.value })}
                placeholder='{"API_TOKEN":"..."}'
                value={form.env}
              />
            </>
          ) : (
            <>
              <input
                aria-label="HTTP URL"
                onChange={(event) => updateForm({ url: event.target.value })}
                placeholder="https://example.com/mcp"
                value={form.url}
              />
              <textarea
                aria-label="HTTP headers JSON"
                onChange={(event) => updateForm({ headers: event.target.value })}
                placeholder='{"Authorization":"Bearer ..."}'
                value={form.headers}
              />
            </>
          )}
          <input
            aria-label="工具超时秒数"
            inputMode="numeric"
            onChange={(event) =>
              updateForm({ toolCallTimeoutSeconds: event.target.value })
            }
            value={form.toolCallTimeoutSeconds}
          />
          <div className="memory-actions">
            <button
              disabled={!form.name.trim() || (form.transport === "stdio" && !form.command.trim()) || (form.transport === "http" && !form.url.trim())}
              onClick={() => void createServer()}
              type="button"
            >
              保存并连接
            </button>
          </div>
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
      {!loading && !loadError && servers.length === 0 ? (
        <EmptyState
          desc="添加一个本地 stdio MCP 服务器或 HTTP MCP 服务，工具会自动进入助手工具列表。"
          title="还没有 MCP 服务器"
        />
      ) : null}
      {servers.map((server) => (
        <div className="memory-item knowledge-item" key={server.id}>
          <p className="memory-content knowledge-title">{server.name}</p>
          <div className="memory-meta">
            <span className="knowledge-badge">{server.transport}</span>
            <span className={`mcp-state is-${server.state}`}>{server.state}</span>
            <span className="knowledge-badge">{server.toolCount} 个工具</span>
          </div>
          <p className="memory-content mcp-command">
            {server.transport === "stdio"
              ? [server.command, ...server.args].filter(Boolean).join(" ")
              : server.url}
          </p>
          {server.lastError ? (
            <p className="proposal-error" role="alert">
              {server.lastError}
            </p>
          ) : null}
          {server.tools.length > 0 ? (
            <div className="mcp-tools">
              {server.tools.map((tool) => (
                <div className="mcp-tool" key={tool.publicName}>
                  <strong>{tool.publicName}</strong>
                  <span>{tool.description}</span>
                </div>
              ))}
            </div>
          ) : null}
          <div className="memory-actions">
            <button
              disabled={busyId === server.id}
              onClick={() => void toggleEnabled(server)}
              type="button"
            >
              {server.enabled ? "禁用" : "启用"}
            </button>
            <button
              disabled={busyId === server.id}
              onClick={() => void reloadServer(server)}
              type="button"
            >
              重连
            </button>
            <button
              className="danger-action"
              disabled={busyId === server.id}
              onClick={() => void removeServer(server)}
              type="button"
            >
              删除
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
