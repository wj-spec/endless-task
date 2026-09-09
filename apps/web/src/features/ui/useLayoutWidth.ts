import { useCallback, useEffect, useState } from "react";

/** S13：可拖拽并记忆的布局宽度（左侧会话栏 / 右侧边栏）。 */

export type LayoutPane = "rail" | "aux";

export const LAYOUT_LIMITS: Record<LayoutPane, { min: number; max: number; fallback: number }> = {
  rail: { min: 200, max: 420, fallback: 252 },
  aux: { min: 280, max: 720, fallback: 440 },
};

const STORAGE_KEY: Record<LayoutPane, string> = {
  rail: "endless-task-rail-width",
  aux: "endless-task-aux-width",
};

const CSS_VAR: Record<LayoutPane, string> = {
  rail: "--rail-w",
  aux: "--aux-w",
};

export const clampWidth = (pane: LayoutPane, value: number): number => {
  const { min, max } = LAYOUT_LIMITS[pane];
  return Math.min(max, Math.max(min, Math.round(value)));
};

const readStored = (pane: LayoutPane): number => {
  const { fallback } = LAYOUT_LIMITS[pane];
  if (typeof window === "undefined") return fallback;
  const stored = Number.parseFloat(localStorage.getItem(STORAGE_KEY[pane]) ?? "");
  return Number.isFinite(stored) ? clampWidth(pane, stored) : fallback;
};

/**
 * 宽度状态：写入 `document.documentElement` 的 CSS 变量（布局栅格直接消费），
 * 同时持久化到 localStorage；双击复位到默认值。
 */
export function useLayoutWidth(pane: LayoutPane): {
  width: number;
  setWidth: (next: number) => void;
  reset: () => void;
} {
  const [width, setWidthState] = useState(() => readStored(pane));

  useEffect(() => {
    setWidthState(readStored(pane));
  }, [pane]);

  useEffect(() => {
    if (typeof document === "undefined") return;
    document.documentElement.style.setProperty(CSS_VAR[pane], `${width}px`);
    if (typeof window !== "undefined") {
      localStorage.setItem(STORAGE_KEY[pane], String(width));
    }
  }, [pane, width]);

  const setWidth = useCallback(
    (next: number) => setWidthState(clampWidth(pane, next)),
    [pane],
  );

  const reset = useCallback(() => {
    setWidthState(LAYOUT_LIMITS[pane].fallback);
  }, [pane]);

  return { width, setWidth, reset };
}
