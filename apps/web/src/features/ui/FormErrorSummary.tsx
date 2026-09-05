import { useEffect, useRef } from "react";

type FormErrorSummaryProps = {
  error: string | string[] | null | undefined;
  heading?: string;
  className?: string;
};

/**
 * 表单级错误汇总：提交失败时置顶展示并把焦点移到汇总区，
 * 方便键盘/读屏用户立即感知与修正（配套的字段级错误用 aria-describedby 关联）。
 */
export function FormErrorSummary({
  error,
  heading = "提交没有完成",
  className,
}: FormErrorSummaryProps) {
  const ref = useRef<HTMLDivElement>(null);
  const messages =
    typeof error === "string"
      ? [error]
      : Array.isArray(error)
        ? error.filter((item): item is string => Boolean(item))
        : [];

  useEffect(() => {
    if (messages.length > 0) ref.current?.focus();
  }, [messages.join("\n")]);

  if (messages.length === 0) return null;

  return (
    <div
      className={className ?? "form-error-summary"}
      ref={ref}
      role="alert"
      tabIndex={-1}
    >
      <strong>{heading}</strong>
      <ul>
        {messages.map((message, index) => (
          <li key={`${index}-${message}`}>{message}</li>
        ))}
      </ul>
    </div>
  );
}
