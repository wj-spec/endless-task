import { useId, useRef } from "react";
import { useModalDialog } from "./useModalDialog";

type ConfirmDialogProps = {
  title: string;
  body: string;
  confirmLabel: string;
  onConfirm: () => void;
  onClose: () => void;
};

export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  onConfirm,
  onClose,
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
        className="confirm-panel"
        ref={panelRef}
        role="dialog"
        tabIndex={-1}
      >
        <h3 id={titleId}>{title}</h3>
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
