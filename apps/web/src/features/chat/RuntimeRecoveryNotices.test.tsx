// @vitest-environment jsdom
/**
 * `RuntimeRecoveryNotices`：SSE 重连提示 + 中断运行的恢复卡片。
 *
 * 搬出前 `recoveryPresentation` 的分类表散在 1 500 行组件中间，e2e 只覆盖了其中一两种
 * 组合。这里把 `findings` / `classification` / `action` 的每一种组合直测一遍，顺带钉住
 * "只有 recoverable+resume 才给重新生成"这条产品规则。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeV2RecoveryReport, RuntimeV2Snapshot } from "./apiTypes";
import {
  RuntimeRecoveryNotices,
  recoveryPresentation,
  type RuntimeRecoveryNoticesProps,
} from "./RuntimeRecoveryNotices";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const report = (
  overrides: Partial<RuntimeV2RecoveryReport> = {},
): RuntimeV2RecoveryReport =>
  ({
    runId: "run_1",
    classification: "unrecoverable",
    action: "mark_failed",
    findings: [],
    ...overrides,
  }) as RuntimeV2RecoveryReport;

const snapshotWith = (
  reports: RuntimeV2RecoveryReport[],
): RuntimeV2Snapshot =>
  ({ interruptedRuns: reports, lastEventSeq: 3 }) as RuntimeV2Snapshot;

const baseProps: RuntimeRecoveryNoticesProps = { pending: false };

let container: HTMLDivElement;
let root: Root;

const render = (props: Partial<RuntimeRecoveryNoticesProps> = {}) => {
  act(() => {
    root.render(<RuntimeRecoveryNotices {...baseProps} {...props} />);
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

describe("recoveryPresentation（分类表）", () => {
  it("等待审批/副作用不确定/结果缺失 → 需要确认，且不给重新生成", () => {
    for (const reason of [
      "waiting_approval",
      "tool_side_effect_uncertain",
      "tool_result_missing",
    ]) {
      const view = recoveryPresentation(
        report({ findings: [{ reason } as never], classification: "recoverable", action: "resume" }),
      );
      expect(view.title).toBe("上次操作需要确认");
      expect(view.canRetry).toBe(false);
    }
  });

  it("停止动作留下的中间态 → 不给重新生成", () => {
    const view = recoveryPresentation(
      report({
        findings: [{ reason: "cancellation_pending" } as never],
        classification: "recoverable",
        action: "resume",
      }),
    );
    expect(view.title).toBe("上次停止操作未完成");
    expect(view.canRetry).toBe(false);
  });

  it("可恢复且动作是继续 → 可以重新生成", () => {
    const view = recoveryPresentation(
      report({ classification: "recoverable", action: "resume" }),
    );
    expect(view.title).toBe("上次运行被中断");
    expect(view.canRetry).toBe(true);
  });

  it("不可恢复 → 只提示未能完成，不给重新生成", () => {
    const view = recoveryPresentation(report());
    expect(view.title).toBe("上次运行未能完成");
    expect(view.canRetry).toBe(false);
  });

  it("穷举 classification × action × findings：只有 recoverable+resume 才允许重新生成", () => {
    const classifications = ["recoverable", "unrecoverable", "unknown"];
    const actions = ["resume", "mark_failed", "unknown"];
    const findings = [
      [],
      [{ reason: "waiting_approval" }],
      [{ reason: "tool_side_effect_uncertain" }],
      [{ reason: "tool_result_missing" }],
      [{ reason: "cancellation_pending" }],
    ];
    const allowed: string[] = [];
    for (const classification of classifications) {
      for (const action of actions) {
        for (const group of findings) {
          const view = recoveryPresentation(
            report({
              classification: classification,
              action,
              findings: group as never,
            }),
          );
          if (view.canRetry) {
            allowed.push(`${classification}/${action}/${group.length}`);
          }
        }
      }
    }
    // 注意：末尾那条兜底 return 里的 `canRetry` 表达式其实**永远为假**（能走到那里就
    // 说明 classification/action 不同时满足 recoverable+resume）。这里是"行为口径"的
    // 穷举证明——保留它是因为产品规则就是"只有这一种组合给重新生成"。
    expect(allowed).toEqual(["recoverable/resume/0"]);
  });
});

describe("RuntimeRecoveryNotices（渲染）", () => {
  it("连接正常且没有中断运行时什么都不渲染", () => {
    render({ connectionPhase: "idle", runtimeSnapshot: snapshotWith([]) });
    expect(container.querySelectorAll(".runtime-recovery-card")).toHaveLength(0);
    expect(text()).toBe("");
  });

  it("重连中：显示保留运行的提示，不出现任何按钮", () => {
    render({ connectionPhase: "reconnecting" });
    const card = container.querySelector(".runtime-recovery-card.is-connecting")!;
    expect(card.getAttribute("role")).toBe("status");
    expect(card.getAttribute("aria-live")).toBe("polite");
    expect(text()).toContain("正在恢复连接");
    expect(text()).toContain("现有运行仍被保留，连接恢复前不会重复提交消息。");
    expect(buttons()).toHaveLength(0);
  });

  it("中断运行：可恢复时给「重新生成 / 结束本次运行」两个动作", () => {
    const onResolve = vi.fn();
    render({
      runtimeSnapshot: snapshotWith([
        report({ classification: "recoverable", action: "resume" }),
      ]),
      onResolveRuntimeRecovery: onResolve,
    });
    const card = container.querySelector(".runtime-recovery-card[role='alert']")!;
    expect(card).not.toBeNull();
    expect(text()).toContain("上次运行被中断");
    act(() => {
      buttons().find((item) => item.textContent === "重新生成")!.click();
      buttons().find((item) => item.textContent === "结束本次运行")!.click();
    });
    expect(onResolve).toHaveBeenNthCalledWith(1, "run_1", "retry");
    expect(onResolve).toHaveBeenNthCalledWith(2, "run_1", "mark_failed");
  });

  it("不可恢复时只给「结束本次运行」，且没有回调就不渲染卡片", () => {
    render({ runtimeSnapshot: snapshotWith([report()]), onResolveRuntimeRecovery: vi.fn() });
    expect(buttons().map((item) => item.textContent)).toEqual(["结束本次运行"]);

    render({ runtimeSnapshot: snapshotWith([report()]) });
    expect(container.querySelectorAll(".runtime-recovery-card")).toHaveLength(0);
  });

  it("pending 时动作按钮禁用（等待中不允许再点）", () => {
    render({
      runtimeSnapshot: snapshotWith([
        report({ classification: "recoverable", action: "resume" }),
      ]),
      onResolveRuntimeRecovery: vi.fn(),
      pending: true,
    });
    const all = buttons();
    expect(all).toHaveLength(2);
    expect(all.every((item) => item.disabled)).toBe(true);
  });

  it("多个中断运行各渲染一张卡片", () => {
    render({
      runtimeSnapshot: snapshotWith([
        report({ runId: "run_1", classification: "recoverable", action: "resume" }),
        report({ runId: "run_2" }),
      ]),
      onResolveRuntimeRecovery: vi.fn(),
    });
    expect(container.querySelectorAll(".runtime-recovery-card")).toHaveLength(2);
  });
});
