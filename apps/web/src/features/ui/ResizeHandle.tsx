import { useCallback, useRef, type KeyboardEvent, type PointerEvent } from "react";

export type ResizeHandleProps = {
  /** 当前尺寸（px）。 */
  value: number;
  min: number;
  max: number;
  onChange: (next: number) => void;
  /** 双击复位（可选）。 */
  onReset?: () => void;
  /** 无障碍名称，例如「调整文件树宽度」。 */
  label: string;
  /** true 表示"向左拖拽 = 变大"（右侧面板用）。 */
  invert?: boolean;
  step?: number;
  className?: string;
};

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, value));

/**
 * 通用分隔条：指针拖拽 + 方向键微调 + 双击复位。
 *
 * 只负责"算出新尺寸并回调"，宽度状态由调用方持有（便于持久化）。
 */
export function ResizeHandle({
  value,
  min,
  max,
  onChange,
  onReset,
  label,
  invert = false,
  step = 16,
  className,
}: ResizeHandleProps) {
  const origin = useRef({ x: 0, value: 0 });

  const onPointerDown = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (event.button !== 0) return;
      event.preventDefault();
      origin.current = { x: event.clientX, value };
      event.currentTarget.setPointerCapture(event.pointerId);
    },
    [value],
  );

  const onPointerMove = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      if (!event.currentTarget.hasPointerCapture(event.pointerId)) return;
      const delta = event.clientX - origin.current.x;
      onChange(clamp(origin.current.value + (invert ? -delta : delta), min, max));
    },
    [invert, max, min, onChange],
  );

  const onPointerUp = useCallback((event: PointerEvent<HTMLDivElement>) => {
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }, []);

  const onKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const direction =
        event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
      if (direction === 0) return;
      event.preventDefault();
      const delta = direction * step * (invert ? -1 : 1);
      onChange(clamp(value + delta, min, max));
    },
    [invert, max, min, onChange, step, value],
  );

  return (
    <div
      aria-label={label}
      aria-orientation="vertical"
      aria-valuemax={max}
      aria-valuemin={min}
      aria-valuenow={Math.round(value)}
      className={["resize-handle", className].filter(Boolean).join(" ")}
      onDoubleClick={onReset}
      onKeyDown={onKeyDown}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      role="separator"
      tabIndex={0}
    >
      <span aria-hidden="true" className="resize-handle-grip" />
    </div>
  );
}
