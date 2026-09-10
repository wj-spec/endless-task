// @vitest-environment jsdom
/**
 * `SurfaceBanners`：从 `ChatWorkSurface.tsx`（1 611 行）JSX 里搬出的三个横幅。
 *
 * 搬出去之前，"什么时候显示哪个横幅"这件事只能靠 e2e 顺带覆盖；现在按可见性组合直测，
 * 顺带把文案里容易改坏的部分（`pending` 禁用态、"查看不会改变主线"、其他分支运行时的
 * 两个动作按钮）钉住。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeV2Lane } from "./apiTypes";
import { SurfaceBanners, type SurfaceBannersProps } from "./SurfaceBanners";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const lane = (id: string, displayName: string): RuntimeV2Lane =>
  ({ id, displayName, kind: "persistent_branch", status: "active" }) as RuntimeV2Lane;

const baseProps: SurfaceBannersProps = {
  ephemeral: false,
  parentTitle: null,
  viewingBranch: false,
  currentLaneLabel: "主线",
  currentLane: null,
  mainLaneId: null,
  otherLaneRunning: false,
  runningLaneLabel: "",
  runningLaneId: null,
  runningRunId: null,
  pending: false,
  canOpenRunningLane: false,
  canCancelRunningRun: false,
  canSwitchToMain: false,
  onPromote: () => {},
};

let container: HTMLDivElement;
let root: Root;

const render = (props: Partial<SurfaceBannersProps> = {}) => {
  act(() => {
    root.render(<SurfaceBanners {...baseProps} {...props} />);
  });
};

const text = () => container.textContent ?? "";
const buttons = () => Array.from(container.querySelectorAll("button"));

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("SurfaceBanners", () => {
  it("三个条件都不满足时不渲染任何横幅", () => {
    render();
    expect(container.querySelectorAll(".branch-banner")).toHaveLength(0);
    expect(text()).toBe("");
  });

  it("临时会话：显示来源与「升级为正式」，点击回调生效", () => {
    const onPromote = vi.fn();
    render({ ephemeral: true, parentTitle: "上一段对话", onPromote });
    const banner = container.querySelector(".branch-banner")!;
    expect(banner.getAttribute("role")).toBe("note");
    expect(text()).toContain("临时会话 · 源自《上一段对话》 · 不写入记忆，用完可丢弃");
    act(() => {
      buttons().find((item) => item.textContent === "升级为正式")!.click();
    });
    expect(onPromote).toHaveBeenCalledTimes(1);
  });

  it("没有来源标题时只显示「临时会话」提示", () => {
    render({ ephemeral: true });
    expect(text()).toContain("临时会话 · 不写入记忆，用完可丢弃");
    expect(text()).not.toContain("源自");
  });

  it("正在查看分支：显示分支名与「返回主线 / 设为主线」", () => {
    const onSwitchLane = vi.fn();
    const onPromoteLane = vi.fn();
    const current = lane("lane_a", "探索方案");
    render({
      viewingBranch: true,
      currentLaneLabel: "探索方案",
      currentLane: current,
      mainLaneId: "lane_main",
      canSwitchToMain: true,
      onSwitchLane,
      onPromoteLane,
    });
    expect(text()).toContain("正在查看分支「探索方案」查看不会改变主线。");
    act(() => {
      buttons().find((item) => item.textContent === "返回主线")!.click();
      buttons().find((item) => item.textContent === "设为主线")!.click();
    });
    expect(onSwitchLane).toHaveBeenCalledWith("lane_main");
    expect(onPromoteLane).toHaveBeenCalledWith("lane_a");
  });

  it("查看分支但没有主线可回时，只显示「设为主线」", () => {
    render({
      viewingBranch: true,
      currentLaneLabel: "探索方案",
      currentLane: lane("lane_a", "探索方案"),
      onPromoteLane: () => {},
    });
    expect(buttons().map((item) => item.textContent)).toEqual(["设为主线"]);
  });

  it("其他分支运行中：提示不并行，并给出查看/停止两个动作", () => {
    const onOpenRunningLane = vi.fn();
    const onCancelRunningRun = vi.fn();
    render({
      otherLaneRunning: true,
      runningLaneLabel: "另一条分支",
      runningLaneId: "lane_b",
      runningRunId: "run_b",
      canOpenRunningLane: true,
      canCancelRunningRun: true,
      onOpenRunningLane,
      onCancelRunningRun,
    });
    const banner = container.querySelector(".runtime-conflict-banner")!;
    expect(banner.getAttribute("role")).toBe("status");
    expect(banner.getAttribute("aria-live")).toBe("polite");
    expect(text()).toContain("「另一条分支」正在运行；同一会话暂不支持多分支并行。");
    act(() => {
      buttons().find((item) => item.textContent === "查看运行位置")!.click();
      buttons().find((item) => item.textContent === "停止后继续")!.click();
    });
    expect(onOpenRunningLane).toHaveBeenCalledWith("lane_b");
    expect(onCancelRunningRun).toHaveBeenCalledWith("run_b");
  });

  it("pending 时所有动作按钮禁用（等待中不允许再点）", () => {
    render({
      ephemeral: true,
      viewingBranch: true,
      currentLaneLabel: "探索方案",
      currentLane: lane("lane_a", "探索方案"),
      mainLaneId: "lane_main",
      canSwitchToMain: true,
      onSwitchLane: () => {},
      onPromoteLane: () => {},
      otherLaneRunning: true,
      runningLaneLabel: "另一条分支",
      runningLaneId: "lane_b",
      runningRunId: "run_b",
      canOpenRunningLane: true,
      canCancelRunningRun: true,
      onOpenRunningLane: () => {},
      onCancelRunningRun: () => {},
      pending: true,
    });
    const all = buttons();
    expect(all).toHaveLength(5);
    expect(all.every((item) => item.disabled)).toBe(true);
  });

  it("运行中的横幅缺少对应的可选回调时不显示该动作", () => {
    render({
      otherLaneRunning: true,
      runningLaneLabel: "另一条分支",
      runningLaneId: "lane_b",
      runningRunId: "run_b",
    });
    expect(buttons()).toHaveLength(0);
    expect(text()).toContain("正在运行");
  });
});
