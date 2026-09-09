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

type TerminalTheme = Record<string, string>;

const cssVar = (name: string, fallback: string): string => {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  return value || fallback;
};

/** 从产品 token 取色构造 xterm 主题（明暗主题都跟随产品，不再有黑底块）。 */
export const buildTerminalTheme = (): TerminalTheme => ({
  background: cssVar("--surface-2", "#f3f3ee"),
  foreground: cssVar("--ink-raise", "#30332f"),
  cursor: cssVar("--accent", "#275c4b"),
  cursorAccent: cssVar("--surface-2", "#f3f3ee"),
  selectionBackground: cssVar("--accent-soft", "#dcebe4"),
  black: cssVar("--ink", "#3c3f3a"),
  red: cssVar("--danger", "#a33b36"),
  green: cssVar("--success", "#4c8a70"),
  yellow: cssVar("--warn-deep", "#6d5a33"),
  blue: cssVar("--accent", "#275c4b"),
  magenta: cssVar("--accent", "#275c4b"),
  cyan: cssVar("--success", "#4c8a70"),
  white: cssVar("--muted-strong", "#55554e"),
  brightBlack: cssVar("--faint", "#8b8b84"),
  brightRed: cssVar("--danger", "#a33b36"),
  brightGreen: cssVar("--success", "#4c8a70"),
  brightYellow: cssVar("--warn-deep", "#6d5a33"),
  brightBlue: cssVar("--accent", "#275c4b"),
  brightMagenta: cssVar("--accent", "#275c4b"),
  brightCyan: cssVar("--success", "#4c8a70"),
  brightWhite: cssVar("--ink-raise", "#30332f"),
});

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
  const [connectionFailed, setConnectionFailed] = useState(false);
  const [reconnectNonce, setReconnectNonce] = useState(0);
  const terminalRefForRetry = useRef<(() => void) | null>(null);

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
    let cleanupTheme: (() => void) | null = null;
    // 渲染器（xterm）尚未加载完成时，先把输出缓存下来——连接不依赖渲染器。
    let pending: string[] = [];

    const flush = () => {
      const handle = terminalRef.current;
      if (!handle || pending.length === 0) return;
      const buffered = pending.join("");
      pending = [];
      handle.write(buffered);
    };

    const pushOutput = (data: string) => {
      if (!data) return;
      if (terminalRef.current) terminalRef.current.write(data);
      else pending.push(data);
    };

    const connect = () => {
      const socket = new WebSocket(
        `${WS_BASE()}/workspaces/${workspaceId}/terminals/${sessionId}`,
      );
      socketRef.current = socket;
      socket.onopen = () => {
        attempts = 0;
        setConnectionFailed(false);
        setError(null);
        setStatus("已连接");
        socket.send(JSON.stringify({ type: "ping" }));
        if (hostRef.current) {
          socket.send(
            JSON.stringify({
              type: "resize",
              rows: Math.max(5, Math.floor(hostRef.current.clientHeight / 18)),
              cols: Math.max(20, Math.floor(hostRef.current.clientWidth / 8)),
            }),
          );
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
          pushOutput(frame.scrollback ?? "");
        } else if (frame.type === "output") {
          pushOutput(frame.data);
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
        attempts += 1;
        if (attempts >= 5) {
          // 连续失败不再空转：给出可操作的状态与重试入口。
          setConnectionFailed(true);
          setStatus("连接失败");
          setError(
            "终端连接失败：请确认本地 API 已启动、该会话仍存在，然后点「重试连接」。",
          );
          return;
        }
        setStatus(`连接断开，正在重连…（第 ${attempts} 次）`);
        reconnectTimer = window.setTimeout(connect, Math.min(5000, 500 * attempts));
      };
      socket.onerror = () => {
        // onerror 之后必然触发 onclose，这里只记录，不覆盖状态文案。
        setError((current) => current ?? "终端连接出错。");
      };
    };

    // 1) 先连（连接与渲染器解耦）：状态文案反映真实连接情况。
    connect();

    // 2) 再加载 xterm 渲染器（动态 import，首屏不承担体积）。
    void (async () => {
      try {
        const [{ Terminal }, { FitAddon }] = await Promise.all([
          import("@xterm/xterm"),
          import("@xterm/addon-fit"),
          // xterm 基础样式必须一起加载：选中文本的 span 靠 `.xterm-decoration-top`
          // （z-index:2）压在选择浮层（.xterm-selection z-index:1）之上，否则
          // 选中区域会被不透明的选择色完全盖住文字。
          import("@xterm/xterm/css/xterm.css"),
        ]);
        if (disposed || !hostRef.current) return;
        const terminal = new Terminal({
          convertEol: false,
          cursorBlink: true,
          fontFamily:
            cssVar("--font-mono", "ui-monospace, SFMono-Regular, Menlo, monospace"),
          fontSize: 13,
          lineHeight: 1.45,
          scrollback: 5000,
          theme: buildTerminalTheme(),
        });
        const fit = new FitAddon();
        terminal.loadAddon(fit);
        terminal.open(hostRef.current);
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
        terminal.onData((data) => {
          const socket = socketRef.current;
          if (!socket || socket.readyState !== WebSocket.OPEN) {
            setError((current) => current ?? "终端未连接，输入已丢弃。");
            return;
          }
          socket.send(JSON.stringify({ type: "input", data }));
        });
        const themeObserver = new MutationObserver(() => {
          terminal.options.theme = buildTerminalTheme();
        });
        // 只跟随主题切换（data-theme）；拖拽改 --rail-w/--aux-w 不应重建主题。
        themeObserver.observe(document.documentElement, {
          attributes: true,
          attributeFilter: ["data-theme"],
        });
        cleanupTheme = () => themeObserver.disconnect();
        // 首次布局可能还没算好，下一帧再 fit 一次。
        requestAnimationFrame(() => {
          try {
            fit.fit();
          } catch {
            // 忽略
          }
        });
        flush();
        terminal.focus();
        if (hostRef.current) {
          resizeObserver = new ResizeObserver(() => {
            try {
              fit.fit();
            } catch {
              return;
            }
            const socket = socketRef.current;
            if (hostRef.current && socket?.readyState === WebSocket.OPEN) {
              socket.send(
                JSON.stringify({
                  type: "resize",
                  rows: Math.max(5, Math.floor(hostRef.current.clientHeight / 18)),
                  cols: Math.max(20, Math.floor(hostRef.current.clientWidth / 8)),
                }),
              );
            }
          });
          resizeObserver.observe(hostRef.current);
        }
      } catch (cause: unknown) {
        if (!disposed) setError(readableError(cause) || "终端组件加载失败。");
      }
    })();

    terminalRefForRetry.current = () => {
      setConnectionFailed(false);
      setError(null);
      setStatus("连接中…");
      setReconnectNonce((value) => value + 1);
    };

    return () => {
      disposed = true;
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
      cleanupTheme?.();
      resizeObserver?.disconnect();
      socketRef.current?.close();
      socketRef.current = null;
      terminalRef.current?.dispose();
      terminalRef.current = null;
      terminalRefForRetry.current = null;
    };
  }, [sessionId, send, workspaceId, reconnectNonce]);

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
        {connectionFailed ? (
          <button
            className="terminal-pane-action is-primary"
            onClick={() => terminalRefForRetry.current?.()}
            type="button"
          >
            重试连接
          </button>
        ) : (
          <button
            className="terminal-pane-action"
            onClick={() => terminalRef.current?.focus()}
            type="button"
          >
            聚焦
          </button>
        )}
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
