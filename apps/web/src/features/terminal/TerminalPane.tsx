import { useCallback, useEffect, useRef, useState } from "react";
import { chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import type { TerminalSnapshot } from "../chat/apiTypes";

type TerminalFrame =
  | { type: "ready"; terminal: TerminalSnapshot; scrollback: string }
  | { type: "output"; data: string }
  | { type: "status"; status: TerminalSnapshot["status"] }
  | { type: "exit"; code: number | null; signal: string | null }
  | { type: "pong" }
  | { type: "error"; message: string };

type XtermHandle = {
  write: (data: string) => void;
  fit: () => void;
  dispose: () => void;
  focus: () => void;
};

const WS_BASE = (): string => {
  const configured = import.meta.env.VITE_API_BASE_URL as string | undefined;
  if (configured && configured.length > 0) {
    return configured.replace(/^http/, "ws").replace(/\/$/, "");
  }
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}`;
};

/**
 * S4 内嵌终端：xterm.js 渲染 + WebSocket 双向通道。
 *
 * xterm 按需动态加载（首屏不承担体积）；输出是原始字节（含转义序列），
 * 全屏程序（vim/htop）由前端仿真，与模型侧的行模式读取互不影响。
 */
export function TerminalPane({
  workspaceId,
  sessionId,
  onClose,
}: {
  workspaceId: string;
  sessionId: string;
  onClose: () => void;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const terminalRef = useRef<XtermHandle | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const [status, setStatus] = useState<string>("连接中…");
  const [error, setError] = useState<string | null>(null);
  const [exitInfo, setExitInfo] = useState<string | null>(null);

  const send = useCallback((frame: Record<string, unknown>) => {
    const socket = socketRef.current;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(frame));
    }
  }, []);

  useEffect(() => {
    let disposed = false;
    let resizeObserver: ResizeObserver | null = null;
    let reconnectTimer: number | null = null;
    let attempts = 0;

    const connect = () => {
      const socket = new WebSocket(
        `${WS_BASE()}/workspaces/${workspaceId}/terminals/${sessionId}`,
      );
      socketRef.current = socket;
      socket.onopen = () => {
        attempts = 0;
        setStatus("已连接");
        send({ type: "ping" });
        const handle = terminalRef.current;
        if (handle && hostRef.current) {
          handle.fit();
          send({
            type: "resize",
            rows: Math.max(5, Math.floor(hostRef.current.clientHeight / 18)),
            cols: Math.max(20, Math.floor(hostRef.current.clientWidth / 8)),
          });
        }
      };
      socket.onmessage = (event) => {
        let frame: TerminalFrame;
        try {
          frame = JSON.parse(String(event.data)) as TerminalFrame;
        } catch {
          return;
        }
        if (frame.type === "ready") {
          setStatus("运行中");
          setExitInfo(null);
          if (frame.scrollback) terminalRef.current?.write(frame.scrollback);
        } else if (frame.type === "output") {
          terminalRef.current?.write(frame.data);
        } else if (frame.type === "exit") {
          setStatus("已退出");
          setExitInfo(
            `进程已退出${frame.code !== null ? `（退出码 ${frame.code}）` : ""}`,
          );
        } else if (frame.type === "error") {
          setError(frame.message);
        }
      };
      socket.onclose = () => {
        if (disposed) return;
        setStatus("连接断开，正在重连…");
        attempts += 1;
        reconnectTimer = window.setTimeout(connect, Math.min(5000, 500 * attempts));
      };
      socket.onerror = () => {
        setError("终端连接出错。");
      };
    };

    void (async () => {
      try {
        const [{ Terminal }, { FitAddon }] = await Promise.all([
          import("@xterm/xterm"),
          import("@xterm/addon-fit"),
        ]);
        if (disposed || !hostRef.current) return;
        const terminal = new Terminal({
          convertEol: false,
          cursorBlink: true,
          fontFamily:
            'ui-monospace, SFMono-Regular, "SF Mono", Menlo, monospace',
          fontSize: 13,
          scrollback: 5000,
          theme: { background: "transparent" },
        });
        const fit = new FitAddon();
        terminal.loadAddon(fit);
        terminal.open(hostRef.current);
        fit.fit();
        terminal.onData((data) => send({ type: "input", data }));
        terminalRef.current = {
          write: (data) => terminal.write(data),
          fit: () => {
            try {
              fit.fit();
            } catch {
              // 容器尚未布局时忽略
            }
          },
          dispose: () => terminal.dispose(),
          focus: () => terminal.focus(),
        };
        terminalRef.current.focus();
        if (hostRef.current) {
          resizeObserver = new ResizeObserver(() => {
            terminalRef.current?.fit();
            if (hostRef.current) {
              send({
                type: "resize",
                rows: Math.max(5, Math.floor(hostRef.current.clientHeight / 18)),
                cols: Math.max(20, Math.floor(hostRef.current.clientWidth / 8)),
              });
            }
          });
          resizeObserver.observe(hostRef.current);
        }
        connect();
      } catch (cause: unknown) {
        if (!disposed) setError(readableError(cause) || "终端组件加载失败。");
      }
    })();

    return () => {
      disposed = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      resizeObserver?.disconnect();
      socketRef.current?.close();
      socketRef.current = null;
      terminalRef.current?.dispose();
      terminalRef.current = null;
    };
  }, [sessionId, send, workspaceId]);

  return (
    <div className="terminal-pane">
      <div className="terminal-pane-head">
        <span className="terminal-pane-status">{status}</span>
        {exitInfo ? <span className="terminal-pane-exit">{exitInfo}</span> : null}
        <button
          className="terminal-pane-action"
          onClick={() => send({ type: "signal", signal: "SIGINT" })}
          type="button"
        >
          中断 (Ctrl+C)
        </button>
        <button
          className="terminal-pane-action"
          onClick={() => terminalRef.current?.focus()}
          type="button"
        >
          聚焦
        </button>
        <button className="terminal-pane-action" onClick={onClose} type="button">
          关闭
        </button>
      </div>
      {error ? (
        <p className="file-viewer-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="terminal-pane-host" ref={hostRef} />
      <p className="terminal-pane-hint">
        该终端内的命令不会逐条确认；关闭标签会结束会话。
      </p>
    </div>
  );
}

export type TerminalPaneListProps = {
  workspaceId: string;
  sessions: TerminalSnapshot[];
  activeId: string | null;
  busy: boolean;
  error: string | null;
  onCreate: () => void;
  onSelect: (sessionId: string) => void;
  onCloseSession: (sessionId: string) => void;
};

/** 终端页签外壳：会话列表 + 当前会话。 */
export function TerminalWorkspace({
  workspaceId,
  sessions,
  activeId,
  busy,
  error,
  onCreate,
  onSelect,
  onCloseSession,
}: TerminalPaneListProps) {
  const active = sessions.find((item) => item.sessionId === activeId) ?? null;
  return (
    <div className="workspace-terminals">
      <div className="workspace-files-toolbar">
        <button disabled={busy} onClick={onCreate} type="button">
          {busy ? "正在创建…" : "新建终端"}
        </button>
        <button
          disabled={!activeId}
          onClick={() => activeId && onCloseSession(activeId)}
          type="button"
        >
          关闭当前
        </button>
      </div>
      {sessions.length > 0 ? (
        <div aria-label="终端会话" className="terminal-tabs" role="tablist">
          {sessions.map((item) => (
            <button
              aria-selected={item.sessionId === activeId}
              className={item.sessionId === activeId ? "is-active" : undefined}
              key={item.sessionId}
              onClick={() => onSelect(item.sessionId)}
              role="tab"
              type="button"
            >
              {item.name || item.sessionId.slice(-6)}
              {item.status.kind === "exited" ? " ·已退出" : ""}
            </button>
          ))}
        </div>
      ) : null}
      {error ? (
        <p className="file-viewer-error" role="alert">
          {error}
        </p>
      ) : null}
      <div className="workspace-terminals-body">
        {active ? (
          <TerminalPane
            key={active.sessionId}
            onClose={() => onCloseSession(active.sessionId)}
            sessionId={active.sessionId}
            workspaceId={workspaceId}
          />
        ) : (
          <p className="file-tree-note">
            还没有终端会话，点「新建终端」在工作区目录启动一个。
          </p>
        )}
      </div>
    </div>
  );
}
