import { CopyButton } from "./CopyButton";
import { skillCommandsOf } from "../skills/skillCommand";

/**
 * 用户消息行（含就地编辑）。
 *
 * 从 `ChatWorkSurface.tsx`（1 315 行）的轮次渲染块里整块搬出（行为零改动，JSX 与文案
 * 逐字保持）。搬出方式是"先局部、后成文件"：上一轮把 661 行一次性搬走导致轮次列表
 * 静默变空（`tsc` 与 330 条 jsdom 用例全绿也拦不住，靠真浏览器才发现），所以这轮改成
 * 一块一块搬，每块都有独立的 props 边界。
 *
 * 编辑态相关的状态（`editTurnId`/`editDraft` 与两个 setter）由 `useTurnInteractions`
 * 提供，这里只负责渲染与回调，不持有状态。
 */
export type UserMessageRowProps = {
  /** 该轮次的用户消息 id 与原文。 */
  turnId: string;
  content: string;
  /** 编辑后的文案覆盖（按轮次 id 存）。 */
  editedContent?: string;
  /** 该轮所属会话（用于判断"只有本会话的最新一轮可编辑"）。 */
  belongsToConversation: boolean;
  isLatest: boolean;
  status: string;
  laneBusy: boolean;
  isGenerating: boolean;
  pending: boolean;
  editing: boolean;
  editDraft: string;
  onEditDraftChange: (value: string) => void;
  onCancelEdit: () => void;
  onStartEdit: (value: string) => void;
  onEditResend?: (turnId: string, content: string) => void;
};

export function UserMessageRow({
  turnId,
  content,
  editedContent,
  belongsToConversation,
  isLatest,
  status,
  laneBusy,
  isGenerating,
  pending,
  editing,
  editDraft,
  onEditDraftChange,
  onCancelEdit,
  onStartEdit,
  onEditResend,
}: UserMessageRowProps) {
  const shown = editedContent ?? content;
  return (
    <article className="message-row user-row">
      <div className="speaker-mark user-mark">你</div>
      <div className="user-row-body">
        {editing ? (
          <>
            <textarea
              aria-label="编辑这条消息"
              className="user-edit-input"
              onChange={(event) => onEditDraftChange(event.target.value)}
              rows={3}
              value={editDraft}
            />
            <div className="user-edit-actions">
              <button
                disabled={
                  editDraft.trim().length === 0 ||
                  editDraft.trim() === shown ||
                  pending ||
                  laneBusy ||
                  isGenerating
                }
                onClick={() => {
                  onEditResend?.(turnId, editDraft.trim());
                  onCancelEdit();
                }}
                type="button"
              >
                保存并重新生成
              </button>
              <button onClick={onCancelEdit} type="button">
                取消
              </button>
            </div>
          </>
        ) : (
          <>
            {skillCommandsOf(shown).length > 0 ? (
              <div className="user-skill-chips">
                {skillCommandsOf(shown).map((name) => (
                  <span className="user-skill-chip" key={name}>
                    已加载技能 /{name}
                  </span>
                ))}
              </div>
            ) : null}
            <div className="user-copy">{shown}</div>
            <div className="user-row-actions">
              <CopyButton ariaLabel="复制这条消息" text={shown} />
              {belongsToConversation &&
              isLatest &&
              onEditResend &&
              !["created", "running"].includes(status) ? (
                <button
                  className="user-edit-trigger"
                  disabled={laneBusy || isGenerating || pending}
                  onClick={() => onStartEdit(shown)}
                  type="button"
                >
                  编辑
                </button>
              ) : null}
            </div>
          </>
        )}
      </div>
    </article>
  );
}
