import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { MoreIcon } from "./Icons";

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

const enabledMenuItems = (menu: HTMLDivElement | null) =>
  Array.from(
    menu?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]:not(:disabled)') ?? [],
  );

export function RowMenu({
  items,
  trigger = <MoreIcon />,
  triggerClassName,
  triggerAriaLabel = "更多操作",
  placement = "down",
  className,
  disabled = false,
}: RowMenuProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const initialFocusRef = useRef<"first" | "last">("first");
  const menuId = useId();

  const returnFocus = () => {
    requestAnimationFrame(() => {
      if (!document.querySelector('[role="dialog"][aria-modal="true"]')) {
        triggerRef.current?.focus();
      }
    });
  };

  useEffect(() => {
    if (!open) return;

    const menuItems = enabledMenuItems(menuRef.current);
    const target =
      initialFocusRef.current === "last" ? menuItems.at(-1) : menuItems[0];
    target?.focus();

    const onPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  const openFromKeyboard = (
    event: ReactKeyboardEvent<HTMLButtonElement>,
  ) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    initialFocusRef.current = event.key === "ArrowUp" ? "last" : "first";
    setOpen(true);
  };

  const handleMenuKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      returnFocus();
      return;
    }

    const menuItems = enabledMenuItems(menuRef.current);
    if (!menuItems.length) return;
    const currentIndex = menuItems.findIndex((item) => item === document.activeElement);
    let targetIndex: number | null = null;

    if (event.key === "ArrowDown") {
      targetIndex = currentIndex < 0 ? 0 : (currentIndex + 1) % menuItems.length;
    } else if (event.key === "ArrowUp") {
      targetIndex =
        currentIndex < 0
          ? menuItems.length - 1
          : (currentIndex - 1 + menuItems.length) % menuItems.length;
    } else if (event.key === "Home") {
      targetIndex = 0;
    } else if (event.key === "End") {
      targetIndex = menuItems.length - 1;
    }

    if (targetIndex !== null) {
      event.preventDefault();
      menuItems[targetIndex]?.focus();
    }
  };

  const rootClass = ["row-menu", placement === "up" ? "pop-up" : "", className ?? ""]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={rootClass} ref={rootRef}>
      <button
        aria-controls={menuId}
        aria-expanded={open}
        aria-haspopup="menu"
        aria-label={triggerAriaLabel}
        className={triggerClassName}
        disabled={disabled}
        onClick={() => {
          initialFocusRef.current = "first";
          setOpen((current) => !current);
        }}
        onKeyDown={openFromKeyboard}
        ref={triggerRef}
        type="button"
      >
        {trigger}
      </button>
      {open ? (
        <div
          aria-label={triggerAriaLabel}
          className="row-menu-pop"
          id={menuId}
          onKeyDown={handleMenuKeyDown}
          ref={menuRef}
          role="menu"
        >
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
                triggerRef.current?.focus();
                item.onSelect();
              }}
              role="menuitem"
              tabIndex={-1}
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
