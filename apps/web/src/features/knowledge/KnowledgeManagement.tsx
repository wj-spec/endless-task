import { useCallback, useEffect, useRef, useState } from "react";
import { EmptyState } from "../ui/EmptyState";
import { chatApi } from "../chat/api";
import type { KnowledgeSource, Workspace } from "../chat/apiTypes";
import { formatRelativeTime } from "../artifacts/time";
import { AddIcon } from "../ui/Icons";

type StatusFilter = "active" | "expired";

type KnowledgeContentProps = {
  workspaceId: string | null;
  workspaces: Workspace[];
};

export function KnowledgeContent({
  workspaceId,
  workspaces,
}: KnowledgeContentProps) {
  const workspaceNameById = new Map(workspaces.map((item) => [item.id, item.name]));
  const [sources, setSources] = useState<KnowledgeSource[]>([]);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftContent, setDraftContent] = useState("");
  const [draftFileName, setDraftFileName] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [draftScope, setDraftScope] = useState<"workspace" | "global">(
    "workspace",
  );
  const fileInputRef = useRef<HTMLInputElement>(null);
  const effectiveScope = workspaceId === null ? "global" : draftScope;
  const draftWorkspaceId = effectiveScope === "global" ? null : workspaceId;

  const load = useCallback(
    async (status: StatusFilter) => {
      setLoading(true);
      try {
        setSources(
          await chatApi.listKnowledgeSources(status, workspaceId ?? "general"),
        );
        setLoadError(null);
      } catch {
        setLoadError("无法加载知识源，请重试。");
      } finally {
        setLoading(false);
      }
    },
    [workspaceId],
  );

  useEffect(() => {
    void load(statusFilter);
  }, [load, statusFilter]);

  const resetDraft = () => {
    setDraftTitle("");
    setDraftContent("");
    setDraftFileName(null);
    setAdding(false);
    setActionError(null);
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const pickFile = async (file: File) => {
    setBusyId("new");
    setActionError(null);
    try {
      const result = await chatApi.importKnowledgeFile(
        file,
        undefined,
        draftWorkspaceId,
      );
      resetDraft();
      await load(statusFilter);
      if (result.truncated) {
        setActionError("文件较大，已截取前 20 万字保存。");
      }
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : "文件导入失败，请重试。",
      );
    } finally {
      setBusyId(null);
    }
  };

  const saveNew = async () => {
    const title = draftTitle.trim();
    const content = draftContent.trim();
    if (!title || !content) return;
    setBusyId("new");
    setActionError(null);
    try {
      await chatApi.createKnowledgeSource({
        kind: draftFileName ? "file" : "note",
        title,
        content,
        fileName: draftFileName ?? undefined,
        workspaceId: draftWorkspaceId,
      });
      resetDraft();
      await load(statusFilter);
    } catch {
      setActionError("保存失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  const saveEdit = async (sourceId: string) => {
    const content = editDraft.trim();
    if (!content) return;
    setBusyId(sourceId);
    setActionError(null);
    try {
      await chatApi.updateKnowledgeSource(sourceId, { content });
      setEditingId(null);
      await load(statusFilter);
    } catch {
      setActionError("保存失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  const toggleExpiry = async (source: KnowledgeSource) => {
    setBusyId(source.id);
    setActionError(null);
    try {
      if (source.status === "active") {
        await chatApi.expireKnowledgeSource(source.id);
      } else {
        await chatApi.restoreKnowledgeSource(source.id);
      }
      await load(statusFilter);
    } catch {
      setActionError("操作失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  const deleteSource = async (sourceId: string) => {
    setBusyId(sourceId);
    setActionError(null);
    try {
      await chatApi.deleteKnowledgeSource(sourceId);
      setConfirmDeleteId(null);
      await load(statusFilter);
    } catch {
      setActionError("删除失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="panel-content">
      <div className="knowledge-toolbar">
        <div className="rail-filter" role="tablist" aria-label="知识源状态">
          <button
            aria-selected={statusFilter === "active"}
            className={statusFilter === "active" ? "is-active" : ""}
            onClick={() => setStatusFilter("active")}
            role="tab"
            type="button"
          >
            活跃
          </button>
          <button
            aria-selected={statusFilter === "expired"}
            className={statusFilter === "expired" ? "is-active" : ""}
            onClick={() => setStatusFilter("expired")}
            role="tab"
            type="button"
          >
            已过期
          </button>
        </div>
        <button
          className="new-chat-button knowledge-add-button"
          onClick={() => (adding ? resetDraft() : setAdding(true))}
          type="button"
        >
          <span aria-hidden="true"><AddIcon size={18} /></span>
          {adding ? "收起" : "添加知识"}
        </button>
      </div>

      {adding ? (
        <div className="knowledge-form">
          <input
            aria-label="知识标题"
            onChange={(event) => setDraftTitle(event.target.value)}
            placeholder="标题，例如：品牌视觉规范"
            value={draftTitle}
          />
          <textarea
            aria-label="知识内容"
            onChange={(event) => setDraftContent(event.target.value)}
            placeholder="写下长期有用的信息；或点「上传文件」直接导入文本文件"
            rows={5}
            value={draftContent}
          />
          <div className="knowledge-scope-picker">
            <label htmlFor="knowledge-scope-select">存入</label>
            <select
              id="knowledge-scope-select"
              onChange={(event) =>
                setDraftScope(event.target.value === "global" ? "global" : "workspace")
              }
              value={effectiveScope}
            >
              {workspaceId !== null ? (
                <option value="workspace">
                  当前工作区{(() => {
                    const name = workspaceNameById.get(workspaceId);
                    return name ? `《${name}》` : "";
                  })()}
                </option>
              ) : null}
              <option value="global">全局知识</option>
            </select>
            {effectiveScope === "global" ? (
              <p className="knowledge-scope-hint">
                全局知识在所有工作区可见，适合关于你本人的长期习惯与生活规则。
              </p>
            ) : null}
          </div>
          <div className="memory-actions">
            <input
              accept=".txt,.md,.markdown,.csv,.tsv,.json,.jsonl,.log"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void pickFile(file);
                event.target.value = "";
              }}
              ref={fileInputRef}
              style={{ display: "none" }}
              type="file"
            />
            <button onClick={() => fileInputRef.current?.click()} type="button">
              上传文件
            </button>
            <button
              disabled={busyId === "new" || !draftTitle.trim() || !draftContent.trim()}
              onClick={() => void saveNew()}
              type="button"
            >
              {busyId === "new" ? "保存中…" : "保存"}
            </button>
            <button onClick={resetDraft} type="button">
              取消
            </button>
          </div>
        </div>
      ) : null}

      {actionError ? (
        <div className="proposal-error" role="alert">
          {actionError}
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
            <button onClick={() => void load(statusFilter)} type="button">
              重试
            </button>
          }
          desc={loadError}
          title="没加载出来"
        />
      ) : null}
      {!loading && !loadError && sources.length === 0 ? (
        <EmptyState
          desc={
            statusFilter === "active"
              ? "添加文件或笔记作为长期资料；对话、记忆与成果已自动纳入检索。"
              : "没有过期的知识源。"
          }
          title={statusFilter === "active" ? "还没有知识源" : "没有过期项"}
        />
      ) : null}
      {sources.map((source) => (
        <div className="memory-item knowledge-item" key={source.id}>
          {editingId === source.id ? (
            <>
              <textarea
                aria-label="编辑知识内容"
                className="memory-edit"
                onChange={(event) => setEditDraft(event.target.value)}
                value={editDraft}
              />
              <div className="memory-actions">
                <button
                  disabled={busyId === source.id || !editDraft.trim()}
                  onClick={() => void saveEdit(source.id)}
                  type="button"
                >
                  {busyId === source.id ? "保存中…" : "保存"}
                </button>
                <button
                  disabled={busyId === source.id}
                  onClick={() => setEditingId(null)}
                  type="button"
                >
                  取消
                </button>
              </div>
            </>
          ) : (
            <>
              <p className="memory-content knowledge-title">{source.title}</p>
              <div className="memory-meta">
                <span className="knowledge-badge">
                  {source.kind === "file" ? "文件" : "笔记"}
                </span>
                <span
                  className={
                    source.workspaceId === null
                      ? "knowledge-badge is-global"
                      : "knowledge-badge is-workspace"
                  }
                >
                  {source.workspaceId === null
                    ? "全局"
                    : workspaceNameById.get(source.workspaceId) ?? "工作区"}
                </span>
                <span
                  className={
                    source.origin === "agent"
                      ? "knowledge-badge is-agent"
                      : "knowledge-badge is-user"
                  }
                >
                  {source.origin === "agent" ? "助手建议" : "你添加"}
                </span>
                {source.status === "expired" ? (
                  <span className="memory-status is-expired">已过期</span>
                ) : null}
                更新于 {formatRelativeTime(source.updatedAt)}
              </div>
              <div className="memory-actions">
                {source.status === "active" ? (
                  <button
                    disabled={busyId === source.id}
                    onClick={() => {
                      setEditingId(source.id);
                      setEditDraft(source.content);
                      setActionError(null);
                    }}
                    type="button"
                  >
                    编辑
                  </button>
                ) : null}
                <button
                  disabled={busyId === source.id}
                  onClick={() => void toggleExpiry(source)}
                  type="button"
                >
                  {source.status === "active" ? "设为过期" : "恢复"}
                </button>
                <button
                  className="danger-action"
                  disabled={busyId === source.id}
                  onClick={() =>
                    setConfirmDeleteId(
                      confirmDeleteId === source.id ? null : source.id,
                    )
                  }
                  type="button"
                >
                  删除
                </button>
              </div>
              {confirmDeleteId === source.id ? (
                <div className="memory-actions">
                  <span>删除后不再参与检索，确定吗？</span>
                  <button
                    className="danger-action"
                    disabled={busyId === source.id}
                    onClick={() => void deleteSource(source.id)}
                    type="button"
                  >
                    {busyId === source.id ? "删除中…" : "确定删除"}
                  </button>
                </div>
              ) : null}
            </>
          )}
        </div>
      ))}
    </div>
  );
}
