import { useEffect, useMemo, useState } from "react";
import { chatApi } from "../chat/api";
import type { WorkspaceFilePreview } from "../chat/apiTypes";
import type { WorkspaceSourceRef } from "./runtimeTrace";

export type WorkspacePreviewTarget = {
  workspaceId: string;
  path: string;
  startLine: number;
  endLine: number;
};

/** 回答下方的"工作区来源"折叠列表：列出本次回答读过的文件与行。 */
export function WorkspaceRefList({
  refs,
  workspaceId,
  onOpen,
}: {
  refs: WorkspaceSourceRef[];
  workspaceId: string;
  onOpen: (ref: WorkspaceSourceRef) => void;
}) {
  const [open, setOpen] = useState(true);
  return (
    <section
      aria-label="回答使用的工作区文件"
      className="workspace-refs"
    >
      <button
        aria-expanded={open}
        className="workspace-refs-toggle"
        onClick={() => setOpen((value) => !value)}
        type="button"
      >
        <span className="workspace-refs-count">
          {refs.length} 个工作区文件
        </span>
        <span aria-hidden="true">{open ? "收起" : "展开"}</span>
      </button>
      {open ? (
        <ul className="workspace-refs-list">
          {refs.map((ref) => (
            <li key={`${ref.toolName}:${ref.path}:${ref.startLine}-${ref.endLine}`}>
              <button
                className="workspace-refs-item"
                disabled={!workspaceId}
                onClick={() => onOpen(ref)}
                type="button"
              >
                <span className="workspace-refs-path">{ref.path}</span>
                <span className="workspace-refs-lines">
                  L{ref.startLine}
                  {ref.endLine !== ref.startLine ? `-L${ref.endLine}` : ""}
                  {ref.totalLines !== null ? `（共 ${ref.totalLines} 行）` : ""}
                  {ref.truncated ? " · 截断" : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

/**
 * 17 切片③：工作区文件预览抽屉 —— 从引用跳转点读取文件行并在抽屉中
 * 展示，目标行范围高亮（ChatGPT 式来源溯源）。
 */
export function WorkspaceFilePreviewDrawer({
  target,
  onClose,
}: {
  target: WorkspacePreviewTarget;
  onClose: () => void;
}) {
  const [preview, setPreview] = useState<WorkspaceFilePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    setError(null);
    setPreview(null);
    void chatApi
      .previewWorkspaceFile(
        target.workspaceId,
        target.path,
        Math.max(1, target.startLine - 20),
        60,
      )
      .then((data) => {
        if (cancelled) return;
        setPreview(data);
      })
      .catch(() => {
        if (cancelled) return;
        setError("文件预览加载失败。");
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [target]);

  const highlightRange = useMemo(
    () => ({ start: target.startLine, end: target.endLine }),
    [target.startLine, target.endLine],
  );

  return (
    <div
      aria-label={`工作区文件 ${target.path} 预览`}
      className="workspace-preview-drawer"
      role="complementary"
    >
      <div className="workspace-preview-head">
        <span className="workspace-preview-title">{target.path}</span>
        <button
          aria-label="关闭文件预览"
          className="workspace-preview-close"
          onClick={onClose}
          type="button"
        >
          ✕
        </button>
      </div>
      {busy ? <p className="workspace-preview-empty">正在读取…</p> : null}
      {error ? (
        <p className="workspace-preview-error" role="alert">
          {error}
        </p>
      ) : null}
      {preview ? (
        <pre className="workspace-preview-code" tabIndex={0}>
          {preview.lines.map((item) => {
            const highlighted =
              item.line >= highlightRange.start && item.line <= highlightRange.end;
            return (
              <div
                className={
                  highlighted
                    ? "workspace-preview-line is-highlight"
                    : "workspace-preview-line"
                }
                data-line={item.line}
                key={item.line}
              >
                <span className="workspace-preview-lineno">{item.line}</span>
                <span className="workspace-preview-text">{item.text || " "}</span>
              </div>
            );
          })}
        </pre>
      ) : null}
    </div>
  );
}
