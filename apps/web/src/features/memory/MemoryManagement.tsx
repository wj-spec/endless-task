import { useCallback, useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import type { MemoryRecord } from "../chat/apiTypes";
import { formatRelativeTime } from "../artifacts/time";

type MemoryManagementProps = {
  onClose: () => void;
};

export function MemoryManagement({ onClose }: MemoryManagementProps) {
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

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

  return (
    <div aria-label="记忆管理" className="overlay" role="dialog">
      <div className="overlay-panel">
        <header className="overlay-header">
          <h2>长期记忆</h2>
          <button onClick={onClose} type="button">
            关闭
          </button>
        </header>
        <div className="overlay-body">
          {actionError ? (
            <div className="proposal-error" role="alert">
              {actionError}
            </div>
          ) : null}
          {loading ? <div className="overlay-empty">正在加载…</div> : null}
          {!loading && loadError ? (
            <div className="overlay-empty">{loadError}</div>
          ) : null}
          {!loading && !loadError && memories.length === 0 ? (
            <div className="overlay-empty">
              在聊天中让助手记住的事会出现在这里。
            </div>
          ) : null}
          {memories.map((memory) => (
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
                    更新于 {formatRelativeTime(memory.updatedAt)}
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
          ))}
        </div>
      </div>
    </div>
  );
}
