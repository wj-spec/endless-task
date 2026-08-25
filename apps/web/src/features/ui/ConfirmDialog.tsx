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
  return (
    <div aria-label={title} className="overlay confirm-scrim" role="dialog">
      <div className="confirm-panel">
        <h3>{title}</h3>
        <p>{body}</p>
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
          <button onClick={onClose} type="button">
            取消
          </button>
        </div>
      </div>
    </div>
  );
}
