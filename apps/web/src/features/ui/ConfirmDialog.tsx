import { useId, useRef } from "react";
import { useModalDialog } from "./useModalDialog";

type ConfirmDialogProps = {
  title: string;
  body: string;
  confirmLabel: string;
  onConfirm: () => void;
  onClose: () => void;
  /** 醒目提示：普通对话框加一个高亮危险提示条。 */
  tone?: "default" | "danger";
  /** 带高亮的提示文案（tone="danger" 时显示）。 */
  warning?: string;
};

export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  onConfirm,
  onClose,
  tone = "default",
  warning,
}: ConfirmDialogProps) {
  const titleId = useId();
  const bodyId = useId();
  const cancelRef = useRef<HTMLButtonElement>(null);
  const panelRef = useModalDialog({ initialFocusRef: cancelRef, onClose });

  return (
    <div
      className="overlay confirm-scrim"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        aria-describedby={bodyId}
        aria-labelledby={titleId}
        aria-modal="true"
        className={`confirm-panel${tone === "danger" ? " is-danger" : ""}`}
        ref={panelRef}
        role="dialog"
        tabIndex={-1}
      >
        <h3 id={titleId}>{title}</h3>
        {tone === "danger" && warning ? (
          <p className="confirm-warning" role="alert">
            {warning}
          </p>
        ) : null}
        <p id={bodyId}>{body}</p>
        <div className="proposal-actions">
          <button
            className="danger-action"
            onClick={() => {
              onConfirm();
              onClose();
            }}
            type="button"
          >
            {confirmLabel}
          </button>
          <button onClick={onClose} ref={cancelRef} type="button">
            取消
          </button>
        </div>
      </div>
    </div>
  );
}
