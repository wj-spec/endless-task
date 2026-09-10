import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { floatingLayerProps } from "./floatingLayer";
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

export type MenuGeometry = {
  triggerRect: { top: number; bottom: number; left: number; right: number };
  menu: { width: number; height: number };
  viewport: { width: number; height: number };
  placement: "down" | "up";
  /** 触发点与菜单之间的间距。 */
  gap?: number;
  /** 视口内边距。 */
  margin?: number;
};

/**
 * 计算浮层菜单的 fixed 定位。
 *
 * 之所以走 fixed + portal：菜单原本绝对定位在触发器里，一旦祖先有
 * `overflow: hidden/auto`（会话栏、标签条）就会被裁掉——这正是
 * 「+ 菜单被挤占」的根因。
 */
export function computeMenuPosition({
  triggerRect,
  menu,
  viewport,
  placement,
  gap = 6,
  margin = 8,
}: MenuGeometry): { top: number; left: number } {
  const maxLeft = Math.max(margin, viewport.width - menu.width - margin);
  const maxTop = Math.max(margin, viewport.height - menu.height - margin);
  // down：右对齐触发点；up：左对齐触发点。
  const preferredLeft =
    placement === "up" ? triggerRect.left : triggerRect.right - menu.width;
  const left = Math.min(Math.max(margin, preferredLeft), maxLeft);
  const preferredTop =
    placement === "up"
      ? triggerRect.top - menu.height - gap
      : triggerRect.bottom + gap;
  const top = Math.min(Math.max(margin, preferredTop), maxTop);
  return { top, left };
}

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
  const [position, setPosition] = useState<{ top: number; left: number } | null>(
    null,
  );
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const initialFocusRef = useRef<"first" | "last">("first");
  const menuId = useId();

  const place = useCallback(() => {
    const trigger = triggerRef.current;
    if (!trigger) return null;
    const rect = trigger.getBoundingClientRect();
    const menu = menuRef.current;
    const next = computeMenuPosition({
      triggerRect: {
        top: rect.top,
        bottom: rect.bottom,
        left: rect.left,
        right: rect.right,
      },
      menu: {
        width: menu?.offsetWidth ?? 160,
        height: menu?.offsetHeight ?? 0,
      },
      viewport: {
        width: window.innerWidth,
        height: window.innerHeight,
      },
      placement,
    });
    setPosition(next);
    return next;
  }, [placement]);

  const focusInitialItem = useCallback(() => {
    const menuItems = enabledMenuItems(menuRef.current);
    const target =
      initialFocusRef.current === "last" ? menuItems.at(-1) : menuItems[0];
    target?.focus();
  }, []);

  // 先挂载再测量：useLayoutEffect 在绘制前完成定位，用户看不到跳动。
  //
  // 注意顺序：**必须先把定位结果写到 DOM，再移焦点**。菜单首次渲染时是
  // `visibility: hidden`（等测量），而 hidden 元素无法获得焦点——此时
  // `focus()` 会静默失败，表现为"菜单开了但焦点还在触发按钮上、方向键失效"
  // （键盘打开菜单的用例就是这么挂的）。这里用 useLayoutEffect 同步落样式，
  // 保证聚焦时元素已经可见。
  useLayoutEffect(() => {
    if (!open) {
      setPosition(null);
      return;
    }
    const next = place();
    const node = menuRef.current;
    if (node && next) {
      node.style.top = `${next.top}px`;
      node.style.left = `${next.left}px`;
      node.style.visibility = "visible";
    }
    focusInitialItem();
  }, [open, place, focusInitialItem]);

  useEffect(() => {
    if (!open) return;
    const onViewportChange = () => place();
    window.addEventListener("resize", onViewportChange);
    window.addEventListener("scroll", onViewportChange, true);
    return () => {
      window.removeEventListener("resize", onViewportChange);
      window.removeEventListener("scroll", onViewportChange, true);
    };
  }, [open, place]);

  const returnFocus = () => {
    requestAnimationFrame(() => {
      if (!document.querySelector('[role="dialog"][aria-modal="true"]')) {
        triggerRef.current?.focus();
      }
    });
  };

  useEffect(() => {
    if (!open) return;

    // 聚焦已在 useLayoutEffect 里完成（必须晚于"定位 + 可见"）。
    const onPointerDown = (event: MouseEvent) => {
      const target = event.target as Node;
      if (rootRef.current?.contains(target)) return;
      // 菜单已 portal 到 body，必须单独判断，否则点菜单会被当成"点外部"。
      if (menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  const openFromKeyboard = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
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
    const currentIndex = menuItems.findIndex(
      (item) => item === document.activeElement,
    );
    let targetIndex: number | null = null;

    if (event.key === "ArrowDown") {
      targetIndex =
        currentIndex < 0 ? 0 : (currentIndex + 1) % menuItems.length;
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

  const rootClass = [
    "row-menu",
    placement === "up" ? "pop-up" : "",
    className ?? "",
  ]
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
      {open && typeof document !== "undefined"
        ? createPortal(
            <div
              {...floatingLayerProps("row-menu")}
              aria-label={triggerAriaLabel}
              className="row-menu-pop is-floating"
              id={menuId}
              onKeyDown={handleMenuKeyDown}
              ref={menuRef}
              role="menu"
              style={
                {
                  top: position?.top ?? 0,
                  left: position?.left ?? 0,
                  visibility: position ? "visible" : "hidden",
                } as CSSProperties
              }
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
            </div>,
            document.body,
          )
        : null}
    </div>
  );
}
