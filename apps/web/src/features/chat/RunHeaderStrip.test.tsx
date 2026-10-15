// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { RuntimeV2RunState, RuntimeV2UsageSummary } from "./apiTypes";
import type { RuntimeConnectionPhase } from "./runtimeController";
import { RunHeaderStrip } from "./RunHeaderStrip";

/**
 * 切片 10 T4（item 1）验收 ①：头部条在一次真运行里 elapsed **会走**、工具/成本随
 * 快照变、断开时相位变"重连中"。
 *
 * 时钟用注入的 `now` + 假定时器驱动：不用 sleep 真等 1 秒，也能证明"没有新事件、
 * 没有新快照，秒数自己在走"。反过来，终态必须**停住**（这是"停止后还在涨"的能红点）。
 */
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const T0 = Date.parse("2026-09-14T10:00:00Z");

const runState = (
  overrides: Partial<RuntimeV2RunState> = {},
): RuntimeV2RunState => ({
  runId: "run_1",
  status: "running",
  partialContent: "",
  errorCode: null,
  safeMessage: null,
  modelTurns: [],
  ...overrides,
});

const usage = (overrides: Partial<RuntimeV2UsageSummary> = {}): RuntimeV2UsageSummary =>
  ({
    runId: "run_1",
    turns: 3,
    inputTokens: 1200,
    outputTokens: 300,
    totalTokens: 1500,
    costUsd: 0.02,
    costPriced: true,
    unpricedTurns: 0,
    priceRevision: null,
    models: ["m"],
    durationMs: 4200,
    firstTokenLatencyMs: null,
    ...overrides,
  }) as RuntimeV2UsageSummary;

let container: HTMLDivElement;
let root: Root;
let clock: { value: number };
const now = () => clock.value;

const render = async (
  state: RuntimeV2RunState | null,
  options: {
    usage?: RuntimeV2UsageSummary | null;
    connectionPhase?: RuntimeConnectionPhase;
  } = {},
) => {
  await act(async () => {
    root.render(
      <RunHeaderStrip
        connectionPhase={options.connectionPhase ?? "connected"}
        now={now}
        runState={state}
        usage={options.usage ?? null}
      />,
    );
  });
  await act(async () => {
    await Promise.resolve();
  });
};

const value = (selector: string) =>
  container.querySelector(selector)?.textContent ?? null;

beforeEach(() => {
  vi.useFakeTimers();
  clock = { value: T0 };
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
});

describe("run 头部条", () => {
  it("没有 run 时整条不渲染", async () => {
    await render(null);
    expect(container.querySelector(".run-header")).toBeNull();
  });

  it("运行中：elapsed 自己会走（没有新事件、没有新快照）", async () => {
    await render(
      runState({
        status: "running",
        startedAt: "2026-09-14T09:59:00Z",
        elapsedSeconds: 60,
        currentStep: {
          modelTurnIndex: 0,
          modelTurnCount: 1,
          toolName: "run_shell",
          toolStatus: "running",
          toolTurnIndex: 0,
          toolInFlight: true,
        },
      }),
      { usage: usage() },
    );

    expect(container.querySelector(".run-header")?.getAttribute("data-run-status")).toBe(
      "running",
    );
    expect(value(".run-header-elapsed .run-header-value")).toBe("1 分");
    expect(value(".run-header-step .run-header-value")).toBe("第 1 轮 · 正在执行 run_shell");
    expect(value(".run-header-usage .run-header-value")).toBe("1.5K tokens · ~$0.0200");
    expect(value(".run-header-phase .run-header-value")).toBe("已连接");

    clock.value = T0 + 5_000;
    await act(async () => {
      vi.advanceTimersByTime(5_000);
    });

    expect(value(".run-header-elapsed .run-header-value")).toBe("1 分 5 秒");
  });

  it("终态：时钟停在后端冻结值，不再跟时间走", async () => {
    await render(
      runState({
        status: "completed",
        startedAt: "2026-09-14T09:50:00Z",
        elapsedSeconds: 42,
      }),
      { connectionPhase: "idle" },
    );

    expect(value(".run-header-status .run-header-value")).toBe("已完成");
    expect(value(".run-header-elapsed .run-header-value")).toBe("42 秒");
    // 运行结束：连接相位回到 idle = 已停止，且时钟不再走。
    expect(value(".run-header-phase .run-header-value")).toBe("已停止");

    clock.value = T0 + 600_000;
    await act(async () => {
      vi.advanceTimersByTime(600_000);
    });

    expect(value(".run-header-elapsed .run-header-value")).toBe("42 秒");
  });

  it("断开时相位变「重连中」（含 connecting 与未知）", async () => {
    await render(runState(), { connectionPhase: "reconnecting" });
    expect(value(".run-header-phase .run-header-value")).toBe("重连中");
    expect(container.querySelector(".run-header")?.getAttribute("data-phase")).toBe(
      "reconnecting",
    );

    await render(runState(), { connectionPhase: "connecting" });
    expect(value(".run-header-phase .run-header-value")).toBe("连接中");

    await act(async () => {
      root.render(
        <RunHeaderStrip now={now} runState={runState()} />,
      );
    });
    expect(value(".run-header-phase .run-header-value")).toBe("未知");
  });

  it("后端没给起点 / 没给预算 / 没给用量时都显示—，不编数字", async () => {
    await render(
      runState({ status: "created", startedAt: null, elapsedSeconds: null }),
    );

    expect(value(".run-header-elapsed .run-header-value")).toBe("—");
    expect(value(".run-header-step .run-header-value")).toBe("尚未开始");
    expect(value(".run-header-usage .run-header-value")).toBe("—");
    // 没有 run.budget_declared：整块预算条不出现（不退回默认档）。
    expect(container.querySelector(".run-header-budget")).toBeNull();
  });

  it("用量未定价时如实显示未定价，并渲染执行器声明的预算墙", async () => {
    await render(
      runState({
        elapsedSeconds: 60,
        budget: {
          hardSeconds: 3600,
          softSeconds: 1800,
          perTurnSeconds: 120,
          costCapUsd: 5,
          usedSeconds: 60,
          remainingSeconds: 3540,
          source: "run_budget_declared",
        },
      }),
      {
        usage: usage({ costUsd: null, costPriced: false, unpricedTurns: 3 }),
      },
    );

    expect(value(".run-header-usage .run-header-value")).toBe("1.5K tokens · 未定价");
    expect(value(".run-header-budget .run-header-value")).toBe(
      "预算剩余 59 分（上限 1 小时） · 单轮 2 分 · 成本上限 $5",
    );
    expect(container.querySelector(".run-header")?.className).toContain("is-running");
  });

  it("换 run 时时钟立刻按新 run 重算（不沿用上一个 run 的秒数）", async () => {
    await render(
      runState({ runId: "run_a", startedAt: "2026-09-14T09:59:00Z" }),
    );
    expect(value(".run-header-elapsed .run-header-value")).toBe("1 分");

    await render(
      runState({ runId: "run_b", startedAt: "2026-09-14T09:59:50Z" }),
    );
    expect(value(".run-header-elapsed .run-header-value")).toBe("10 秒");
  });
});
