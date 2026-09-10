// @vitest-environment jsdom
/**
 * `RuntimeStatusStrip`：从 `ChatWorkSurface.tsx`（1 459 行）搬出的运行状态条。
 *
 * 这里钉住三件事：① 各组件的出现条件（含"升级优先于卡住"这条二选一规则）；
 * ② 传下去的 `pending` 会让按钮禁用；③ 审计面板拿到的刷新键/运行 id 来自父层算好的值
 * （搬迁前是散在 JSX 里的 `runtimeSnapshot?.lastEventSeq ?? null` 之类的表达式）。
 *
 * `AuditTrailPanel` 会调 `chatApi`，这里统一桩掉，避免测试打真请求。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  RuntimeV2Escalation,
  RuntimeV2StuckState,
  RuntimeV2UsageSummary,
  RuntimeV2Verification,
} from "./apiTypes";
import {
  RuntimeStatusStrip,
  type RuntimeStatusStripProps,
} from "./RuntimeStatusStrip";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

// `vi.mock` 的工厂会被提升到文件顶部，所以计数用的 mock 必须放在 `vi.hoisted` 里
// （第一版写成普通 const，vitest 直接报 "Cannot access ... before initialization"）。
const { listAuditTrail } = vi.hoisted(() => ({
  listAuditTrail: vi.fn(async () => ({ entries: [] })),
}));
vi.mock("./api", () => ({
  ApiClientError: class extends Error {},
  chatApi: new Proxy(
    { getRunAuditTrail: listAuditTrail },
    {
      get: (target: Record<string, unknown>, prop: string) =>
        target[prop] ?? (() => Promise.resolve({ items: [] })),
    },
  ),
}));

const usage: RuntimeV2UsageSummary = {
  inputTokens: 1200,
  outputTokens: 300,
  costUsd: 0.02,
  costPriced: true,
  unpricedTurns: 0,
  durationMs: 4200,
} as RuntimeV2UsageSummary;

const verification: RuntimeV2Verification = {
  runId: "run_1",
  status: "verified",
  verdict: "fail",
  reasons: ["报告未写入"],
  missing: ["report.md"],
  model: "verifier-model",
};

const escalation: RuntimeV2Escalation = {
  runId: "run_1",
  reason: "no_progress",
  summary: "连续 4 轮没有实质进展（工具调用 5 次、失败 3 次，新增文本 42 字）。",
  options: ["continue", "change_approach", "take_over"],
  progress: {
    modelTurns: 4,
    toolCalls: 5,
    toolFailures: 3,
    producedCharacters: 42,
    inputTokens: 900,
    outputTokens: 100,
  },
  budget: { usedTokens: 1000, limitTokens: 900, usedRatio: 1 },
  repeatedFailures: [
    {
      toolName: "read_file",
      errorCode: "temporary_unavailable",
      count: 3,
      safeMessage: "工具暂时不可用。",
    },
  ],
  failures: [],
  guidance: "失败记忆：read_file 已连续失败 3 次。请更换方法或参数，不要原样重复同一调用。",
  willStop: false,
};

const stuck: RuntimeV2StuckState = {
  runId: "run_1",
  level: "remind",
  detector: "repeated_failure",
  reasons: ["`read_file` 已连续失败 2 次（temporary_unavailable）"],
  repeatedFailures: [
    {
      toolName: "read_file",
      errorCode: "temporary_unavailable",
      count: 2,
      safeMessage: "工具暂时不可用。",
    },
  ],
  attempts: [
    {
      toolName: "read_file",
      errorCode: "temporary_unavailable",
      attempt: 2,
      toolExecutionId: "exec_2",
    },
  ],
  guidance: "失败记忆：`read_file` 已连续失败 2 次。请更换方法或参数，不要原样重复同一调用。",
};

const baseProps: RuntimeStatusStripProps = {
  pending: false,
  lastEventSeq: 12,
  activeRunId: "run_1",
};

let container: HTMLDivElement;
let root: Root;

const render = (props: Partial<RuntimeStatusStripProps> = {}) => {
  act(() => {
    root.render(<RuntimeStatusStrip {...baseProps} {...props} />);
  });
};

const text = () => container.textContent ?? "";
const buttons = () => Array.from(container.querySelectorAll("button"));

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  listAuditTrail.mockClear();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("RuntimeStatusStrip", () => {
  it("没有任何状态时只有审计面板（它自取数据，不依赖其它状态）", () => {
    render();
    expect(container.querySelector(".usage-meter")).toBeNull();
    expect(container.querySelector('[aria-label="需要你决定下一步"]')).toBeNull();
    expect(text()).toContain("审计轨迹");
  });

  it("有用量时渲染用量条，但不渲染验证/升级/卡住", () => {
    render({ usage });
    expect(container.querySelector(".usage-meter")).not.toBeNull();
    expect(text()).not.toContain("独立验证");
    expect(container.querySelector('[aria-label="需要你决定下一步"]')).toBeNull();
  });

  it("升级状态优先于卡住状态（二选一，不重复打扰）", () => {
    render({ escalationState: escalation, stuckState: stuck });
    expect(
      container.querySelector('[aria-label="需要你决定下一步"]'),
    ).not.toBeNull();
    expect(text()).not.toContain("已连续失败 2 次"); // 卡住提示的文案不出现
  });

  it("只有卡住状态时退到卡住提示", () => {
    render({ stuckState: stuck });
    expect(text()).toContain("已连续失败 2 次");
    expect(
      container.querySelector('[aria-label="需要你决定下一步"]'),
    ).toBeNull();
  });

  it("验证未通过时展示理由与两条出路，pending 时按钮禁用", () => {
    render({
      verificationState: verification,
      pending: true,
      onSteerRun: vi.fn(),
      onCancelRunningRun: vi.fn(),
    });
    expect(text()).toContain("独立验证：未通过");
    expect(text()).toContain("报告未写入");
    const all = buttons();
    expect(all.map((item) => item.textContent)).toEqual(
      expect.arrayContaining(["带结论重试", "人工接管"]),
    );
    // 验证徽标的两个动作在 pending 时禁用；审计面板的展开按钮不属于"动作"，
    // 它只是本地展开/收起，不受 pending 影响（排查时按按钮清单确认过）。
    const actions = all.filter((item) => item.textContent !== "审计轨迹（为什么这么做）");
    expect(actions.every((item) => item.disabled)).toBe(true);
  });

  it("展开审计面板后按父层算好的运行 id 取数据", async () => {
    render({ lastEventSeq: 99, activeRunId: "run_42" });
    // 面板是"点开才拉取"（`useEffect` 里有 `if (!open || !runId) return`），
    // 所以先展开再断言——第一版直接断言调用，看到的是"从未请求"。
    act(() => {
      buttons()
        .find((item) => item.textContent === "审计轨迹（为什么这么做）")!
        .click();
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(listAuditTrail).toHaveBeenCalledWith("run_42");
  });

  it("没有运行 id 时审计面板整体不渲染（不会有可点的入口，也就不会空查）", async () => {
    render({ activeRunId: null });
    expect(
      buttons().find((item) => item.textContent === "审计轨迹（为什么这么做）"),
    ).toBeUndefined();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(listAuditTrail).not.toHaveBeenCalled();
  });
});
