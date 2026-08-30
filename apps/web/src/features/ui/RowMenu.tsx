import { useEffect, useRef, useState, type ReactNode } from "react";

export type RowMenuItem = {
  label: string;
  onSelect: () => void;
  disabled?: boolean;
  danger?: boolean;
  className?: string;
};

type RowMenuProps = {
  items: RowMenuItem[];
  trigger?: ReactNode;
  triggerClassName?: string;
  triggerAriaLabel?: string;
  placement?: "down" | "up";
  className?: string;
  disabled?: boolean;
};

export function RowMenu({
  items,
  trigger = "⋯",
  triggerClassName,
  triggerAriaLabel = "更多操作",
  placement = "down",
  className,
  disabled = false,
}: RowMenuProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const rootClass = ["row-menu", placement === "up" ? "pop-up" : "", className ?? ""]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={rootClass} ref={ref}>
      <button
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={triggerAriaLabel}
        className={triggerClassName}
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        type="button"
      >
        {trigger}
      </button>
      {open ? (
        <div className="row-menu-pop" role="menu">
          {items.map((item) => (
            <button
              className={
                [item.danger ? "danger-action" : "", item.className ?? ""]
                  .filter(Boolean)
                  .join(" ") || undefined
              }
              disabled={item.disabled}
              key={item.label}
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
              role="menuitem"
              type="button"
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
