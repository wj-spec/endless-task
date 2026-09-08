import { useCallback, useEffect, useState } from "react";
import { EmptyState } from "../ui/EmptyState";
import { chatApi } from "../chat/api";
import type { MemoryRecord } from "../chat/apiTypes";
import { formatRelativeTime } from "../artifacts/time";

type MemoryContentProps = {
  onOpenConversation: (conversationId: string) => void;
};

const PROFILE_GROUPS = [
  { kind: "preference", label: "偏好" },
  { kind: "fact", label: "事实" },
];

export function MemoryContent({ onOpenConversation }: MemoryContentProps) {
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [showExpired, setShowExpired] = useState(false);

  const load = useCallback(async () => {
    try {
      setMemories(await chatApi.listMemories());
      setLoadError(null);
    } catch {
      setLoadError("无法加载记忆，请重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const startEdit = (memory: MemoryRecord) => {
    setEditingId(memory.id);
    setEditDraft(memory.content);
    setActionError(null);
  };

  const saveEdit = async (memoryId: string) => {
    const content = editDraft.trim();
    if (!content) return;
    setBusyId(memoryId);
    setActionError(null);
    try {
      await chatApi.updateMemory(memoryId, content);
      setEditingId(null);
      await load();
    } catch {
      setActionError("保存失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  const deleteMemory = async (memoryId: string) => {
    setBusyId(memoryId);
    setActionError(null);
    try {
      await chatApi.deleteMemory(memoryId);
      setConfirmDeleteId(null);
      await load();
    } catch {
      setActionError("删除失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  const renderMemory = (memory: MemoryRecord) => (
    <div className="memory-item" key={memory.id}>
      {editingId === memory.id ? (
        <>
          <textarea
            aria-label="编辑记忆内容"
            className="memory-edit"
            onChange={(event) => setEditDraft(event.target.value)}
            value={editDraft}
          />
          <div className="memory-actions">
            <button
              disabled={busyId === memory.id || !editDraft.trim()}
              onClick={() => void saveEdit(memory.id)}
              type="button"
            >
              {busyId === memory.id ? "保存中…" : "保存"}
            </button>
            <button
              disabled={busyId === memory.id}
              onClick={() => setEditingId(null)}
              type="button"
            >
              取消
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="memory-content">{memory.content}</p>
          <div className="memory-meta">
            {memory.status === "expired" ? (
              <span className="memory-status is-expired">已过期</span>
            ) : null}
            {memory.sourceConversationTitle ? (
              <button
                className="memory-source"
                onClick={() => onOpenConversation(memory.sourceConversationId)}
                type="button"
              >
                来自「{memory.sourceConversationTitle}」
              </button>
            ) : (
              <span className="memory-source is-unattributed" title="这条记忆没有可溯源的来源">
                未溯源
              </span>
            )}
            <span>更新于 {formatRelativeTime(memory.updatedAt)}</span>
          </div>
          {memory.status === "active" ? (
            <div className="memory-actions">
              <button
                disabled={busyId === memory.id}
                onClick={() => startEdit(memory)}
                type="button"
              >
                编辑
              </button>
              <button
                className="danger-action"
                disabled={busyId === memory.id}
                onClick={() =>
                  setConfirmDeleteId(
                    confirmDeleteId === memory.id ? null : memory.id,
                  )
                }
                type="button"
              >
                {confirmDeleteId === memory.id ? "取消" : "删除"}
              </button>
            </div>
          ) : null}
          {confirmDeleteId === memory.id ? (
            <div className="memory-confirm">
              <p>删除后助手将不再记住这条信息，且无法恢复。确认删除？</p>
              <div className="memory-actions">
                <button
                  className="danger-action"
                  disabled={busyId === memory.id}
                  onClick={() => void deleteMemory(memory.id)}
                  type="button"
                >
                  {busyId === memory.id ? "删除中…" : "确认删除"}
                </button>
                <button
                  disabled={busyId === memory.id}
                  onClick={() => setConfirmDeleteId(null)}
                  type="button"
                >
                  取消
                </button>
              </div>
            </div>
          ) : null}
        </>
      )}
    </div>
  );

  const active = memories.filter((memory) => memory.status === "active");
  const expired = memories.filter((memory) => memory.status !== "active");

  return (
    <div className="panel-content">
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
            <button onClick={() => void load()} type="button">
              重试
            </button>
          }
          desc={loadError}
          title="没加载出来"
        />
      ) : null}
      {!loading && !loadError && memories.length === 0 ? (
        <EmptyState
          desc="在聊天中明确告诉助手的偏好和关于你的事实，经你确认后会出现这里。"
          title="助手会慢慢认识你"
        />
      ) : null}
      {!loading && !loadError
        ? PROFILE_GROUPS.map((group) => {
            const items = active.filter((memory) => memory.kind === group.kind);
            if (items.length === 0) return null;
            return (
              <section className="profile-group" key={group.kind}>
                <h3 className="profile-group-title">
                  {group.label}（{items.length}）
                </h3>
                {items.map(renderMemory)}
              </section>
            );
          })
        : null}
      {!loading && !loadError && expired.length > 0 ? (
        <section className="profile-group">
          <button
            className="profile-expired-toggle"
            onClick={() => setShowExpired((value) => !value)}
            type="button"
          >
            {showExpired ? "收起已过期" : `已过期（${expired.length}）`}
          </button>
          {showExpired ? expired.map(renderMemory) : null}
        </section>
      ) : null}
    </div>
  );
}
