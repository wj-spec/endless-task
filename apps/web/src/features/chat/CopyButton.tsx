import { useCallback, useRef, useState } from "react";
import { writeClipboard } from "./clipboard";

type CopyButtonProps = {
  text: string;
  label?: string;
  copiedLabel?: string;
  /** 提供给读屏的完整操作名，如 “复制这段回答”。省略时用 label。 */
  ariaLabel?: string;
  className?: string;
};

export function CopyButton({
  text,
  label = "复制",
  copiedLabel = "已复制",
  ariaLabel,
  className,
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<number | null>(null);

  const handleCopy = useCallback(async () => {
    await writeClipboard(text);
    setCopied(true);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setCopied(false), 2000);
  }, [text]);

  return (
    <button
      type="button"
      aria-label={copied ? copiedLabel : (ariaLabel ?? label)}
      className={className ?? "copy-button"}
      onClick={() => void handleCopy()}
    >
      {copied ? copiedLabel : label}
    </button>
  );
}
