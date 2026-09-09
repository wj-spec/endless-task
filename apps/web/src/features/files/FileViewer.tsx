import { useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import type { WorkspaceFilePreview } from "../chat/apiTypes";
import { CodeBlock } from "../chat/CodeBlock";
import { MessageContent } from "../chat/MessageContent";
import { isMarkdownPath, languageForPath } from "./workspaceFiles";

const PREVIEW_LINE_LIMIT = 2000;

export type FilePreviewBodyProps = {
  path: string;
  preview: WorkspaceFilePreview | null;
  busy: boolean;
  error: string | null;
  onReveal?: () => void;
};

/** 纯展示：给定预览数据渲染 Markdown / 代码 / 带行号纯文本。 */
export function FilePreviewBody({
  path,
  preview,
  busy,
  error,
  onReveal,
}: FilePreviewBodyProps) {
  if (busy) return <p className="file-viewer-note">正在读取…</p>;
  if (error) {
    return (
      <div className="file-viewer-error" role="alert">
        <p>{error}</p>
        {onReveal ? (
          <button onClick={onReveal} type="button">
            用系统程序打开
          </button>
        ) : null}
      </div>
    );
  }
  if (!preview) return null;

  const text = preview.lines.map((line) => line.text).join("\n");
  const truncated = preview.endLine < preview.totalLines;
  const hint = truncated ? (
    <p className="file-viewer-note">
      文件较大，仅预览前 {preview.endLine} 行（共 {preview.totalLines} 行）。
    </p>
  ) : null;

  if (isMarkdownPath(path)) {
    return (
      <div className="file-viewer-markdown">
        <MessageContent content={text} />
        {hint}
      </div>
    );
  }

  const language = languageForPath(path);
  if (language) {
    return (
      <div className="file-viewer-code">
        <CodeBlock code={text} language={language} />
        {hint}
      </div>
    );
  }

  return (
    <div className="file-viewer-plain">
      <pre className="file-viewer-lines" tabIndex={0}>
        {preview.lines.map((item) => (
          <div className="file-viewer-line" data-line={item.line} key={item.line}>
            <span className="file-viewer-lineno">{item.line}</span>
            <span className="file-viewer-text">{item.text || " "}</span>
          </div>
        ))}
      </pre>
      {hint}
    </div>
  );
}

export type WorkspaceFileViewerProps = {
  workspaceId: string;
  path: string;
  rootPath?: string | null;
  onBack: () => void;
  onEdit?: () => void;
};

/** P0 文件面板：单个文件的只读预览（工作区根内）。 */
export function WorkspaceFileViewer({
  workspaceId,
  path,
  rootPath,
  onBack,
  onEdit,
}: WorkspaceFileViewerProps) {
  const [preview, setPreview] = useState<WorkspaceFilePreview | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    setError(null);
    setPreview(null);
    void chatApi
      .previewWorkspaceFile(workspaceId, path, 1, PREVIEW_LINE_LIMIT)
      .then((data) => {
        if (!cancelled) setPreview(data);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(readableError(cause) || "文件预览加载失败。");
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [path, workspaceId]);

  const reveal = rootPath
    ? () => {
        void chatApi.revealInFinder(`${rootPath}/${path}`).catch(() => undefined);
      }
    : undefined;

  return (
    <div className="file-viewer">
      <div className="file-viewer-head">
        <button
          aria-label="返回文件树"
          className="file-viewer-back"
          onClick={onBack}
          type="button"
        >
          ← 文件
        </button>
        <span className="file-viewer-path" title={path}>
          {path}
        </span>
        {onEdit ? (
          <button className="file-viewer-action" onClick={onEdit} type="button">
            编辑
          </button>
        ) : null}
        {reveal ? (
          <button
            className="file-viewer-action"
            onClick={reveal}
            type="button"
          >
            在访达中显示
          </button>
        ) : null}
      </div>
      <div className="file-viewer-body">
        <FilePreviewBody
          busy={busy}
          error={error}
          onReveal={reveal}
          path={path}
          preview={preview}
        />
      </div>
    </div>
  );
}
