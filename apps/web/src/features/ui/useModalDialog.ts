import { useEffect, useRef, type RefObject } from "react";

const focusableSelector = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

const getDialogFocusable = (container: HTMLElement | null) => {
  if (!container) return [] as HTMLElement[];
  return Array.from(container.querySelectorAll<HTMLElement>(focusableSelector)).filter(
    (element) => {
      if (element.hasAttribute("disabled")) return false;
      if (element.getAttribute("tabindex") === "-1") return false;
      // Radio groups only expose the checked radio to the tab order; a stray
      // unchecked radio must not count as the trailing focusable, otherwise
      // the trap never wraps and focus escapes the dialog.
      if (
        element instanceof HTMLInputElement &&
        element.type === "radio" &&
        !element.checked
      ) {
        return false;
      }
      return true;
    },
  );
};

type ModalDialogOptions = {
  active?: boolean;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onClose: () => void;
};

export function useModalDialog<T extends HTMLElement = HTMLDivElement>({
  active = true,
  initialFocusRef,
  onClose,
}: ModalDialogOptions) {
  const dialogRef = useRef<T>(null);
  const closeRef = useRef(onClose);

  closeRef.current = onClose;

  useEffect(() => {
    if (!active) return;

    const returnFocusTarget =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    (initialFocusRef?.current ?? dialogRef.current)?.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
        return;
      }
      if (event.key !== "Tab") return;

      const dialog = dialogRef.current;
      const focusable = getDialogFocusable(dialog);
      if (!dialog || !focusable.length) {
        event.preventDefault();
        dialog?.focus();
        return;
      }

      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!dialog.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (returnFocusTarget?.isConnected) returnFocusTarget.focus();
    };
  }, [active, initialFocusRef]);

  return dialogRef;
}