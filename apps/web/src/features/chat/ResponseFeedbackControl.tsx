import { useState } from "react";
import { chatApi } from "./api";
import type { FeedbackRating } from "./apiTypes";

const REASONS = ["太啰嗦", "理解错了", "不该这么做", "其他"];

type ResponseFeedbackControlProps = {
  turnId: string;
  variantId: string | null;
  conversationId: string | null;
  disabled?: boolean;
};

function ThumbIcon({ up }: { up: boolean }) {
  return (
    <svg aria-hidden="true" fill="none" height={14} viewBox="0 0 24 24" width={14}>
      {up ? (
        <path
          d="M7 10v11M7 10l4-7a2.5 2.5 0 0 1 2.5 2.5V10h5a2 2 0 0 1 2 2.4l-1.2 7A2 2 0 0 1 17.3 21H7"
          stroke="currentColor"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="1.8"
        />
      ) : (
        <path
          d="M17 14V3M17 14l-4 7a2.5 2.5 0 0 1-2.5-2.5V14h-5a2 2 0 0 1-2-2.4l1.2-7A2 2 0 0 1 6.7 3H17"
          stroke="currentColor"
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth="1.8"
        />
      )}
    </svg>
  );
}

export function ResponseFeedbackControl({
  turnId,
  variantId,
  conversationId,
  disabled = false,
}: ResponseFeedbackControlProps) {
  const [rating, setRating] = useState<FeedbackRating | null>(null);
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (value: FeedbackRating, submittedReason: string, submittedNote: string) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await chatApi.recordFeedback(turnId, {
        rating: value,
        reason: submittedReason || undefined,
        note: submittedNote || undefined,
        variantId: variantId ?? undefined,
        conversationId: conversationId ?? undefined,
      });
      setRating(value);
      setOpen(false);
      setReason("");
      setNote("");
    } catch {
      setError("反馈提交失败，请重试。");
    } finally {
      setBusy(false);
    }
  };

  const toggle = (value: FeedbackRating) => {
    if (disabled || busy) return;
    if (rating === value) {
      // 再次点击清除
      setRating(null);
      return;
    }
    if (value === "down") {
      setOpen(true);
      return;
    }
    void submit("up", "", "");
  };

  return (
    <div className="response-feedback" role="group" aria-label="反馈这条回答">
      <button
        aria-label="满意"
        aria-pressed={rating === "up"}
        className={`feedback-btn is-up${rating === "up" ? " is-active" : ""}`}
        disabled={disabled || busy}
        onClick={() => toggle("up")}
        title="满意"
        type="button"
      >
        <ThumbIcon up />
      </button>
      <button
        aria-label="不满意"
        aria-pressed={rating === "down"}
        className={`feedback-btn is-down${rating === "down" ? " is-active" : ""}`}
        disabled={disabled || busy}
        onClick={() => toggle("down")}
        title="不满意"
        type="button"
      >
        <ThumbIcon up={false} />
      </button>

      {open ? (
        <div className="feedback-panel" role="dialog" aria-label="反馈原因">
          <div className="feedback-panel-head">
            <span>为什么不满意？</span>
            <button
              aria-label="关闭"
              className="feedback-close"
              disabled={busy}
              onClick={() => setOpen(false)}
              type="button"
            >
              ×
            </button>
          </div>
          <div className="feedback-reasons">
            {REASONS.map((value) => (
              <button
                className={`feedback-reason${reason === value ? " is-selected" : ""}`}
                key={value}
                onClick={() => setReason(value)}
                type="button"
              >
                {value}
              </button>
            ))}
          </div>
          <input
            aria-label="补充说明（可选）"
            className="feedback-note"
            onChange={(event) => setNote(event.target.value)}
            placeholder="补充说明（可选）"
            value={note}
          />
          <div className="feedback-actions">
            <button
              className="feedback-submit"
              disabled={busy}
              onClick={() => void submit("down", reason, note)}
              type="button"
            >
              提交
            </button>
            <button
              className="feedback-cancel"
              disabled={busy}
              onClick={() => setOpen(false)}
              type="button"
            >
              取消
            </button>
          </div>
        </div>
      ) : null}

      {rating ? (
        <span className="feedback-confirm">已评分</span>
      ) : null}
      {error ? <span className="feedback-error">{error}</span> : null}
    </div>
  );
}
