// @vitest-environment jsdom
/**
 * `LineageSummary`：继承内容的摘要条与两种分隔线（从 `ChatWorkSurface.tsx` 搬出）。
 *
 * 临时会话的"继承"语义此前只有 e2e 顺带覆盖，这里把三条文案与展开开关钉住——
 * 尤其"缺少来源标题时回退到「主会话」"这条兜底。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  LineageDivider,
  LineageSummary,
  LineageTailDivider,
} from "./LineageSummary";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const render = (node: React.ReactNode) => {
  act(() => {
    root.render(node);
  });
};

const text = () => container.textContent ?? "";

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("LineageSummary", () => {
  it("显示来源会话与继承轮数，并给出「查看继承内容」", () => {
    render(
      <LineageSummary
        inheritedTurnCount={3}
        onToggle={() => {}}
        open={false}
        parentTitle="来源会话"
      />,
    );
    expect(text()).toContain("继承自《来源会话》");
    expect(text()).toContain("3 轮上下文已随临时会话复制并隔离保存");
    const button = container.querySelector("button")!;
    expect(button.textContent).toBe("查看继承内容");
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });

  it("展开时按钮文案与 aria 状态同步", () => {
    render(
      <LineageSummary
        inheritedTurnCount={1}
        onToggle={() => {}}
        open
        parentTitle="来源会话"
      />,
    );
    const button = container.querySelector("button")!;
    expect(button.textContent).toBe("收起继承内容");
    expect(button.getAttribute("aria-expanded")).toBe("true");
  });

  it("没有来源标题时回退到「主会话」", () => {
    render(
      <LineageSummary
        inheritedTurnCount={0}
        onToggle={() => {}}
        open={false}
        parentTitle={null}
      />,
    );
    expect(text()).toContain("继承自《主会话》");
  });

  it("点击按钮触发切换回调", () => {
    const onToggle = vi.fn();
    render(
      <LineageSummary
        inheritedTurnCount={2}
        onToggle={onToggle}
        open={false}
        parentTitle="来源会话"
      />,
    );
    act(() => {
      container.querySelector("button")!.click();
    });
    expect(onToggle).toHaveBeenCalledTimes(1);
  });
});

describe("两种分隔线", () => {
  it("中段分隔线说明「以下是本会话内容」", () => {
    render(<LineageDivider parentTitle="来源会话" />);
    expect(text()).toBe("以上继承自《来源会话》，以下是本会话内容");
    expect(container.firstElementChild?.getAttribute("role")).toBe("note");
  });

  it("末尾分隔线说明「以上全部继承」并回退标题", () => {
    render(<LineageTailDivider parentTitle={undefined} />);
    expect(text()).toBe("以上全部继承自《主会话》，从这里开始是新内容");
  });
});
