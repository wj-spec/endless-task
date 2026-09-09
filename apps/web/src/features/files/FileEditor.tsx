import { useEffect, useRef, useState } from "react";
import type { Extension } from "@codemirror/state";
import { ApiClientError, chatApi } from "../chat/api";
import { readableError } from "../chat/apiErrorText";
import type {
  WorkspaceFileConflictDetails,
  WorkspaceFileWriteResult,
} from "../chat/apiTypes";
import { MessageContent } from "../chat/MessageContent";
import {
  copyNameFor,
  diffLines,
  diffSummary,
  languageForPath,
  type DiffLine,
} from "./workspaceFiles";

const LCS_LANGUAGE_LOADERS: Record<string, () => Promise<unknown>> = {
  markdown: () => import("@codemirror/lang-markdown"),
  python: () => import("@codemirror/lang-python"),
  javascript: () => import("@codemirror/lang-javascript"),
  json: () => import("@codemirror/lang-json"),
};

type CodeMirrorHandle = {
  destroy: () => void;
  getValue: () => string;
  setValue: (value: string) => void;
};

/**
 * CodeMirror 6 宿主：按文件类型动态加载语言包，编辑器本身也动态 import，
 * 首屏不承担编辑器体积。加载期间显示占位。
 */
export function CodeMirrorEditor({
  value,
  path,
  onChange,
  readOnly = false,
}: {
  value: string;
  path: string;
  onChange: (value: string) => void;
  readOnly?: boolean;
}) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const handleRef = useRef<CodeMirrorHandle | null>(null);
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  const onChangeRef = useRef(onChange);
  const valueRef = useRef(value);
  onChangeRef.current = onChange;
  valueRef.current = value;

  useEffect(() => {
    let disposed = false;
    setReady(false);
    setFailed(false);
    void (async () => {
      try {
        const [core, stateModule] = await Promise.all([
          import("codemirror"),
          import("@codemirror/state"),
        ]);
        const language = languageForPath(path);
        const loader = LCS_LANGUAGE_LOADERS[language];
        const languageModule = loader ? await loader() : null;
        if (disposed || !hostRef.current) return;
        const { EditorView, basicSetup } = core;
        const extensions: Extension[] = [
          basicSetup,
          EditorView.lineWrapping,
          EditorView.updateListener.of((update) => {
            if (update.docChanged) onChangeRef.current(update.state.doc.toString());
          }),
          EditorView.theme({
            "&": { fontSize: "13px", height: "100%" },
            ".cm-scroller": { fontFamily: "var(--font-mono)", overflow: "auto" },
            ".cm-content": { minHeight: "240px" },
            "&.cm-focused": { outline: "none" },
          }),
          stateModule.EditorState.readOnly.of(readOnly),
        ];
        if (languageModule) {
          const factory = (
            languageModule as {
              markdown?: () => Extension;
              python?: () => Extension;
              javascript?: () => Extension;
              json?: () => Extension;
            }
          )[language as "markdown" | "python" | "javascript" | "json"];
          if (factory) extensions.push(factory());
        }
        const view = new EditorView({
          parent: hostRef.current,
          state: stateModule.EditorState.create({
            doc: valueRef.current,
            extensions,
          }),
        });
        if (disposed) {
          view.destroy();
          return;
        }
        handleRef.current = {
          destroy: () => view.destroy(),
          getValue: () => view.state.doc.toString(),
          setValue: (next: string) => {
            const current = view.state.doc.toString();
            if (current === next) return;
            view.dispatch({
              changes: { from: 0, to: current.length, insert: next },
            });
          },
        };
        setReady(true);
      } catch {
        if (!disposed) setFailed(true);
      }
    })();
    return () => {
      disposed = true;
      handleRef.current?.destroy();
      handleRef.current = null;
    };
  }, [path, readOnly]);

  useEffect(() => {
    handleRef.current?.setValue(value);
  }, [value]);

  return (
    <div className="file-editor-host">
      <div className="file-editor-mount" ref={hostRef} />
      {!ready && !failed ? (
        <p className="file-viewer-note">编辑器加载中…</p>
      ) : null}
      {failed ? (
        <p className="file-viewer-error" role="alert">
          编辑器加载失败，请刷新页面重试。
        </p>
      ) : null}
    </div>
  );
}

/** 保存后的行级变更回显。 */
export function FileDiffView({
  before,
  after,
  onClose,
}: {
  before: string;
  after: string;
  onClose: () => void;
}) {
  const lines: DiffLine[] = diffLines(before, after);
  const summary = diffSummary(lines);
  return (
    <div className="file-diff">
      <div className="file-diff-head">
        <span className="file-diff-summary">
          本次保存：+{summary.added} / −{summary.removed}
        </span>
        <button onClick={onClose} type="button">
          收起变更
        </button>
      </div>
      <pre className="file-diff-body" tabIndex={0}>
        {lines.map((line, index) => (
          <div
            className={`file-diff-line is-${line.type}`}
            key={`${index}-${line.type}`}
          >
            <span aria-hidden="true" className="file-diff-sign">
              {line.type === "add" ? "+" : line.type === "remove" ? "−" : " "}
            </span>
            <span className="file-diff-text">{line.text || " "}</span>
          </div>
        ))}
      </pre>
    </div>
  );
}

/** 版本冲突三选一：重新加载 / 覆盖保存 / 另存为副本。 */
export function FileConflictDialog({
  message,
  details,
  busy,
  onReload,
  onOverwrite,
  onSaveCopy,
  onCancel,
}: {
  message: string;
  details: WorkspaceFileConflictDetails | null;
  busy: boolean;
  onReload: () => void;
  onOverwrite: () => void;
  onSaveCopy: () => void;
  onCancel: () => void;
}) {
  return (
    <div aria-live="assertive" className="file-conflict" role="alertdialog">
      <p className="file-conflict-message">{message}</p>
      <p className="file-conflict-hint">
        当前文件已被其它改动更新
        {details?.beforeExists === false ? "（原文件已被删除）" : ""}。
        请选择如何处理你的编辑。
      </p>
      <div className="file-conflict-actions">
        <button disabled={busy} onClick={onReload} type="button">
          重新加载（丢弃我的编辑）
        </button>
        <button disabled={busy} onClick={onOverwrite} type="button">
          覆盖保存
        </button>
        <button disabled={busy} onClick={onSaveCopy} type="button">
          另存为副本
        </button>
        <button disabled={busy} onClick={onCancel} type="button">
          取消
        </button>
      </div>
    </div>
  );
}

export type WorkspaceFileEditorProps = {
  workspaceId: string;
  conversationId: string;
  path: string;
  onBack: () => void;
  onSaved?: (result: WorkspaceFileWriteResult) => void;
};

/** P1：工作区文件编辑（读取 → 编辑 → 保存 → 撤销 / 冲突处理）。 */
export function WorkspaceFileEditor({
  workspaceId,
  conversationId,
  path,
  onBack,
  onSaved,
}: WorkspaceFileEditorProps) {
  const [baseline, setBaseline] = useState("");
  const [draft, setDraft] = useState("");
  const [version, setVersion] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<{
    message: string;
    details: WorkspaceFileConflictDetails | null;
  } | null>(null);
  const [savedInfo, setSavedInfo] = useState<{
    undoEntryId: string | null;
    before: string;
    after: string;
  } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [showPreview, setShowPreview] = useState(false);
  const [showDiff, setShowDiff] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    setError(null);
    setConflict(null);
    setSavedInfo(null);
    setNotice(null);
    void chatApi
      .readWorkspaceFile(workspaceId, path)
      .then((file) => {
        if (cancelled) return;
        setBaseline(file.content);
        setDraft(file.content);
        setVersion(file.version);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(readableError(cause) || "文件读取失败。");
      })
      .finally(() => {
        if (!cancelled) setBusy(false);
      });
    return () => {
      cancelled = true;
    };
  }, [path, workspaceId]);

  const dirty = draft !== baseline;

  const persist = async (
    content: string,
    expectedVersion: string | null,
    targetPath = path,
    fallbackMessage = "保存失败。",
  ): Promise<boolean> => {
    setSaving(true);
    setError(null);
    try {
      const result = await chatApi.writeWorkspaceFile(
        workspaceId,
        targetPath,
        { content, version: expectedVersion },
        conversationId,
      );
      if (targetPath === path) {
        setBaseline(content);
        setDraft(content);
        setVersion(result.version);
        setSavedInfo({
          undoEntryId: result.undoEntryId,
          before: baseline,
          after: content,
        });
        setShowDiff(false);
        setNotice(null);
        setConflict(null);
      } else {
        setNotice(`已另存为 ${result.path}`);
      }
      onSaved?.(result);
      return true;
    } catch (cause: unknown) {
      if (cause instanceof ApiClientError && cause.code === "file_version_conflict") {
        setConflict({
          message: cause.message,
          details: (cause.details ?? null) as WorkspaceFileConflictDetails | null,
        });
        return false;
      }
      setError(readableError(cause) || fallbackMessage);
      return false;
    } finally {
      setSaving(false);
    }
  };

  const reloadFromServer = async () => {
    setSaving(true);
    try {
      const file = await chatApi.readWorkspaceFile(workspaceId, path);
      setBaseline(file.content);
      setDraft(file.content);
      setVersion(file.version);
      setConflict(null);
      setSavedInfo(null);
      setNotice("已载入服务器上的最新内容。");
    } catch (cause: unknown) {
      setError(readableError(cause) || "重新加载失败。");
    } finally {
      setSaving(false);
    }
  };

  const overwrite = async () => {
    const expected = conflict?.details?.currentVersion ?? null;
    await persist(draft, expected, path, "覆盖保存失败。");
  };

  const saveCopy = async () => {
    await persist(draft, null, copyNameFor(path), "另存副本失败。");
  };

  const undo = async () => {
    const entryId = savedInfo?.undoEntryId;
    if (!entryId) return;
    setSaving(true);
    try {
      await chatApi.undoJournalEntry(entryId);
      const file = await chatApi.readWorkspaceFile(workspaceId, path);
      setBaseline(file.content);
      setDraft(file.content);
      setVersion(file.version);
      setSavedInfo(null);
      setNotice("已撤销本次保存。");
    } catch (cause: unknown) {
      setError(readableError(cause) || "撤销失败。");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="file-editor">
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
          {dirty ? <span className="file-editor-dirty">● 未保存</span> : null}
        </span>
        {languageForPath(path) === "markdown" ? (
          <button
            aria-pressed={showPreview}
            className="file-viewer-action"
            onClick={() => setShowPreview((value) => !value)}
            type="button"
          >
            {showPreview ? "只看编辑" : "预览"}
          </button>
        ) : null}
        <button
          className="file-viewer-action is-primary"
          disabled={busy || saving || !dirty}
          onClick={() => void persist(draft, version)}
          type="button"
        >
          {saving ? "保存中…" : "保存"}
        </button>
      </div>
      {notice ? <p className="file-editor-notice">{notice}</p> : null}
      {error ? (
        <p className="file-viewer-error" role="alert">
          {error}
        </p>
      ) : null}
      {conflict ? (
        <FileConflictDialog
          busy={saving}
          details={conflict.details}
          message={conflict.message}
          onCancel={() => setConflict(null)}
          onOverwrite={() => void overwrite()}
          onReload={() => void reloadFromServer()}
          onSaveCopy={() => void saveCopy()}
        />
      ) : null}
      {savedInfo && !conflict ? (
        <div className="file-editor-saved">
          <span>已保存</span>
          {savedInfo.undoEntryId ? (
            <button disabled={saving} onClick={() => void undo()} type="button">
              撤销
            </button>
          ) : null}
          <button onClick={() => setShowDiff((value) => !value)} type="button">
            {showDiff ? "收起变更" : "查看变更"}
          </button>
          <button onClick={() => setSavedInfo(null)} type="button">
            知道了
          </button>
        </div>
      ) : null}
      {showDiff && savedInfo && !conflict ? (
        <FileDiffView
          after={savedInfo.after}
          before={savedInfo.before}
          onClose={() => setShowDiff(false)}
        />
      ) : null}
      <div
        className={`file-editor-body${showPreview ? " is-split" : ""}`}
      >
        {busy ? (
          <p className="file-viewer-note">正在读取…</p>
        ) : (
          <CodeMirrorEditor onChange={setDraft} path={path} value={draft} />
        )}
        {showPreview ? (
          <div className="file-editor-preview">
            <MessageContent content={draft} />
          </div>
        ) : null}
      </div>
    </div>
  );
}
