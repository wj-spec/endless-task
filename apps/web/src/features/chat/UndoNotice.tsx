import { useEffect, useState } from "react";
import { chatApi } from "./api";
import type { UndoJournalEntry } from "./apiTypes";
import {
  isUndoable,
  undoDoneText,
  undoImpactText,
  undoKindLabel,
  undoNoticeText,
} from "./undoJournal";

type UndoNoticeProps = {
  conversationId: string | null;
  /** 运行结束后刷新一次（撤销列表按需拉取，避免每次渲染都请求）。 */
  refreshKey?: string | number | null;
  pending?: boolean;
};

/**
 * A5 撤销/回滚：把"最近一次可逆操作"摆在对话上方，一键回滚。
 *
 * 只对可逆操作（文件写/删）提供入口；不可逆操作（外部动作、命令副作用）
 * 不会出现在这里——它们只能靠 A1 的确认来避免。
 */
export const UndoNotice = ({
  conversationId,
  refreshKey = null,
  pending = false,
}: UndoNoticeProps) => {
  const [entry, setEntry] = useState<UndoJournalEntry | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      if (!conversationId) {
        setEntry(null);
        return;
      }
      try {
        const result = await chatApi.listUndoJournal(conversationId, 5);
        if (cancelled) return;
        setEntry(result.items.find((item) => isUndoable(item)) ?? null);
      } catch {
        if (!cancelled) setEntry(null);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [conversationId, refreshKey]);

  const undo = async () => {
    if (!entry) return;
    setBusy(true);
    setError(null);
    try {
      const result = await chatApi.undoJournalEntry(entry.id);
      setNote(undoDoneText(result.entry, result.alreadyUndone));
      setEntry(null);
    } catch {
      setError("撤销失败，请稍后重试。");
    } finally {
      setBusy(false);
    }
  };

  if (note) {
    return (
      <div className="inline-notice undo-notice" role="status">
        <span>{note}</span>
        <button onClick={() => setNote(null)} type="button">
          知道了
        </button>
      </div>
    );
  }
  if (!entry) return null;
  return (
    <div className="inline-notice undo-notice" role="status">
      <span className="undo-notice-text">
        {undoNoticeText(entry)}
        <span className="undo-notice-impact">（{undoImpactText(entry)}）</span>
      </span>
      <button
        disabled={busy || pending}
        onClick={() => void undo()}
        type="button"
      >
        {busy ? "撤销中…" : `撤销${undoKindLabel(entry.kind)}`}
      </button>
      {error ? <span className="undo-notice-error">{error}</span> : null}
    </div>
  );
};
