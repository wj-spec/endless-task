import { useCallback, useEffect, useRef, type RefObject } from "react";

export type TurnFocusTarget = {
  conversationId: string;
  /** 目标轮次 id（v2 run id；也兼容变体 id 匹配）。 */
  turnId: string;
  /** 同一目标重复点击时的自增序号，用于重新触发定位。 */
  nonce: number;
};

export type TurnFocusOptions = {
  containerRef: RefObject<HTMLElement | null>;
  /** 当前会话 id；未加载时为 undefined（此时不定位）。 */
  conversationId: string | undefined;
  focusTarget: TurnFocusTarget | null;
  /** 已渲染的轮次数；变化时重新尝试定位。 */
  turnCount: number;
  /** 高亮持续时间（毫秒）。 */
  highlightMs?: number;
  /** 定位完成（或放弃）后的回调，用于恢复自动滚底。 */
  onSettled?: () => void;
};

const escapeForSelector = (value: string): string =>
  typeof CSS !== "undefined" && typeof CSS.escape === "function"
    ? CSS.escape(value)
    : value.replace(/["\\]/g, "\\$&");

/**
 * 通知/引用跳转的定位：把目标轮次滚到视口中央并短暂高亮。
 *
 * 轮次是异步映射出来的（运行时快照 → 轮次），因此允许若干帧重试；
 * 定位期间调用方应暂停"自动滚到底"，否则结果会被顶掉。
 */
export function useTurnFocus({
  containerRef,
  conversationId,
  focusTarget,
  turnCount,
  highlightMs = 1800,
  onSettled,
}: TurnFocusOptions): { shouldSuppressAutoScroll: () => boolean } {
  const pendingRef = useRef(false);
  const settledRef = useRef(onSettled);
  settledRef.current = onSettled;

  useEffect(() => {
    pendingRef.current = Boolean(
      focusTarget && focusTarget.conversationId === conversationId,
    );
    if (!pendingRef.current) return;
  }, [conversationId, focusTarget?.nonce, focusTarget]);

  useEffect(() => {
    if (!focusTarget || focusTarget.conversationId !== conversationId) return;
    if (turnCount === 0) return;
    let cancelled = false;
    const target = escapeForSelector(focusTarget.turnId);
    const find = (): HTMLElement | null => {
      const root = containerRef.current;
      if (!root) return null;
      return (
        root.querySelector<HTMLElement>(`[data-turn-id="${target}"]`) ??
        root.querySelector<HTMLElement>(`[data-variant-ids~="${target}"]`)
      );
    };
    const reduceMotion =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let attempts = 0;
    let timer: number | null = null;
    const settle = () => {
      pendingRef.current = false;
      settledRef.current?.();
    };
    const tick = () => {
      if (cancelled) return;
      const node = find();
      if (!node) {
        if (attempts < 12) {
          attempts += 1;
          requestAnimationFrame(tick);
        } else {
          settle();
        }
        return;
      }
      node.scrollIntoView({
        block: "center",
        behavior: reduceMotion ? "auto" : "smooth",
      });
      node.classList.add("is-focus-target");
      timer = window.setTimeout(() => {
        node.classList.remove("is-focus-target");
        settle();
      }, highlightMs);
    };
    requestAnimationFrame(tick);
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
    };
  }, [
    containerRef,
    conversationId,
    focusTarget,
    highlightMs,
    turnCount,
  ]);

  // 稳定的回调：调用方把它放进 effect 依赖时不会因为每次渲染的新对象而反复触发
  // （否则"自动滚到底"会在每次渲染时执行，把定位结果顶掉）。
  const shouldSuppressAutoScroll = useCallback(() => pendingRef.current, []);

  return { shouldSuppressAutoScroll };
}
