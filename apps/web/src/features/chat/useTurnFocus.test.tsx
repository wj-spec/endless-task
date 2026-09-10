// @vitest-environment jsdom
import { act, useEffect, useRef, useState } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useTurnFocus, type TurnFocusTarget } from "./useTurnFocus";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const scrolled: string[] = [];

/** 测试宿主：容器里放两轮，轮次可用 props 控制是否"渲染出来"。 */
function Host({
  focusTarget,
  withTurns = true,
}: {
  focusTarget: TurnFocusTarget | null;
  withTurns?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const focus = useTurnFocus({
    containerRef: ref,
    conversationId: "conv_1",
    focusTarget,
    turnCount: withTurns ? 2 : 0,
    highlightMs: 50,
  });
  // 效果跑完后才能观察到抑制状态（与 ChatWorkSurface 里自动滚底 effect 的时序一致）。
  const [suppress, setSuppress] = useState("no");
  useEffect(() => {
    setSuppress(focus.shouldSuppressAutoScroll() ? "yes" : "no");
  });
  return (
    <div data-suppress={suppress} ref={ref}>
      {withTurns ? (
        <>
          <section className="turn" data-turn-id="run_old" data-variant-ids="run_old">
            旧一轮
          </section>
          <section
            className="turn"
            data-turn-id="run_target"
            data-variant-ids="run_target run_regen"
          >
            目标轮
          </section>
        </>
      ) : null}
    </div>
  );
}

const render = (target: TurnFocusTarget | null, withTurns = true) => {
  act(() => root.render(<Host focusTarget={target} withTurns={withTurns} />));
};

const flushFrames = async () => {
  await act(async () => {
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
    await new Promise((resolve) => requestAnimationFrame(() => resolve(null)));
  });
};

beforeEach(() => {
  scrolled.length = 0;
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  Element.prototype.scrollIntoView = function scrollIntoView() {
    scrolled.push((this as HTMLElement).dataset.turnId ?? "");
  };
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("useTurnFocus（通知定位）", () => {
  it("滚动到目标轮次并短暂高亮", async () => {
    render({ conversationId: "conv_1", turnId: "run_target", nonce: 1 });
    await flushFrames();

    expect(scrolled).toEqual(["run_target"]);
    const node = container.querySelector<HTMLElement>('[data-turn-id="run_target"]')!;
    expect(node.classList.contains("is-focus-target")).toBe(true);
    // 高亮是短暂的
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 80));
    });
    expect(node.classList.contains("is-focus-target")).toBe(false);
  });

  it("turnId 未命中时按变体 id 兜底匹配", async () => {
    render({ conversationId: "conv_1", turnId: "run_regen", nonce: 1 });
    await flushFrames();
    expect(scrolled).toEqual(["run_target"]);
  });

  it("会话不匹配或没有目标时不滚动", async () => {
    render({ conversationId: "conv_other", turnId: "run_target", nonce: 1 });
    await flushFrames();
    expect(scrolled).toEqual([]);

    render(null);
    await flushFrames();
    expect(scrolled).toEqual([]);
  });

  it("定位待处理期间抑制自动滚底", async () => {
    render({ conversationId: "conv_1", turnId: "run_target", nonce: 1 });
    await flushFrames();
    expect(container.firstElementChild?.getAttribute("data-suppress")).toBe("yes");
  });

  it("轮次尚未渲染时重试，渲染后仍能定位", async () => {
    render({ conversationId: "conv_1", turnId: "run_target", nonce: 1 }, false);
    await flushFrames();
    expect(scrolled).toEqual([]);

    render({ conversationId: "conv_1", turnId: "run_target", nonce: 1 }, true);
    await flushFrames();
    expect(scrolled).toEqual(["run_target"]);
  });
});
