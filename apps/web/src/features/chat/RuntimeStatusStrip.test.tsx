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
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  RuntimeV2StuckState,
  RuntimeV2UsageSummary,
  RuntimeV2Verification,
} from "./apiTypes";
import { deriveEscalationState } from "./escalation";
import {
  RuntimeStatusStrip,
  type RuntimeStatusStripProps,
} from "./RuntimeStatusStrip";
import { verificationRetryMessage } from "./verification";

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
  // 默认与夹具的 runId 一致 —— "这条 run 还在飞"。终态场景显式传 null。
  runningRunId: "run_1",
};

/**
 * 从**真实数据流**构造 `escalationState`（批次 C0 复审 Critical-1 的要求）：
 * 喂后端下发形状的 snapshot（`escalation`/`runningRunId`/`activeRunId`）给
 * `deriveEscalationState`，而不是手搓一个与快照自相矛盾的状态。
 *
 * 硬停（`willStop=true`）的升级在后端**特意**保留到 run 终态之后
 * （`views.py`：`if terminal_run and not escalation["willStop"]: return None`），
 * 所以真实存在"快照里有 willStop 升级 + runningRunId 为 null"这一格。
 */
const escalationFromFlow = (runningRunId: string | null) =>
  deriveEscalationState(
    {
      runningRunId,
      activeRunId: runningRunId ?? "run_terminated",
      escalation: { ...escalation, willStop: true },
    } as unknown as RuntimeV2Snapshot,
    undefined,
  )!;

/** 同上，但走"事件流"那条路（内容只来自 `run.awaiting_user`）。 */
const escalationFromEvents = (runningRunId: string | null) =>
  deriveEscalationState(
    { runningRunId } as unknown as RuntimeV2Snapshot,
    [
      {
        eventId: "evt_1",
        eventSeq: 1,
        type: "run.awaiting_user",
        conversationId: "conv_1",
        laneId: "lane_main",
        runId: "run_1",
        createdAt: "2026-09-14T09:00:00Z",
        data: { ...escalation, runId: "run_1" },
      } as unknown as RuntimeV2ProductEvent,
    ],
  )!;

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

  // 切片 10 A2（item 7）在卡住横幅上的收口：两条出路的落点按"该 run 是否还在飞"分支。
  it("卡住横幅（run 仍活跃）：「换一条路径 / 人工接管」照旧走 steer / 取消", () => {
    const onSteerRun = vi.fn();
    const onCancelRunningRun = vi.fn();
    const onStartFreshTurn = vi.fn();
    // activeRunId 默认就是 "run_1"，与 stuck.runId 相同 → 活跃。
    render({
      stuckState: stuck,
      onSteerRun,
      onCancelRunningRun,
      onStartFreshTurn,
    });

    expect(buttons().map((item) => item.textContent)).toEqual(
      expect.arrayContaining(["换一条路径", "人工接管"]),
    );
    act(() => buttons().find((item) => item.textContent === "换一条路径")!.click());
    expect(onStartFreshTurn).not.toHaveBeenCalled();
    expect(onSteerRun).toHaveBeenCalledWith(
      "run_1",
      "已失败：read_file(temporary_unavailable×2)。请换一种方法或参数继续，不要原样重复同一个调用。",
    );
  });

  it("卡住横幅照旧无条件给两条出路（run 还在跑时切走 lane 也不误藏）", () => {
    const onSteerRun = vi.fn();
    const onCancelRunningRun = vi.fn();
    // 真实数据流：`runningRunId` 按**整个会话**取（那条 run 确实还在跑），
    // `activeRunId` 按**当前所选 lane**取 —— 用户切到别的 lane 后两者不同。
    // 卡住横幅不会挂在终态 run 上（`_stuck_json` 在无在飞 run 时返回 None），
    // 所以这里两条出口必须原样保留。
    render({
      activeRunId: "lane_other/run_9",
      runningRunId: "run_1",
      stuckState: stuck,
      onSteerRun,
      onCancelRunningRun,
    });

    expect(buttons().map((item) => item.textContent)).toEqual(
      expect.arrayContaining(["换一条路径", "人工接管"]),
    );
    act(() => buttons().find((item) => item.textContent === "人工接管")!.click());
    expect(onCancelRunningRun).toHaveBeenCalledWith("run_1");
  });

  it(`卡住横幅在读屏名里不谎称「运行已结束」（该状态不可能属于终态 run）`, () => {
    render({ stuckState: stuck, onSteerRun: vi.fn() });
    expect(container.querySelector('[aria-label="运行卡住"]')).not.toBeNull();
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

  // 批次 C0 控制器追加（第 2 个同类"无对象入口"）：`verification` 由
  // `_verification_json(run_id=active_run_id or running_run_id)` 投影，而
  // `active_run_id` 终态后仍在（`replay.py` 的 `lane_runs[-1]` 无 status 过滤），
  // 前端 `deriveVerificationState` 也没有终态清理分支 ⇒ 徽标能活到终态之后。
  it("验证未通过（run 已终态）：「带结论重试」走新开一轮，「人工接管」不渲染", () => {
    const onSteerRun = vi.fn();
    const onCancelRunningRun = vi.fn();
    const onStartFreshTurn = vi.fn();
    render({
      verificationState: verification,
      onCancelRunningRun,
      onStartFreshTurn,
      onSteerRun,
      // 真实数据流：没有在飞 run（run_1 已终态），但快照里 verification 仍在。
      runningRunId: null,
    });

    const labels = buttons().map((item) => item.textContent);
    expect(labels).not.toContain("人工接管");
    const retry = buttons().find((item) => item.textContent === "带结论重试");
    expect(retry).not.toBeUndefined();

    act(() => retry!.click());

    expect(onSteerRun).not.toHaveBeenCalled();
    expect(onCancelRunningRun).not.toHaveBeenCalled();
    // 单实参：适配器丢掉了 `VerificationBadge` 传给回调的 runId。
    expect(onStartFreshTurn).toHaveBeenCalledTimes(1);
    expect(onStartFreshTurn).toHaveBeenCalledWith(
      verificationRetryMessage(verification),
    );
    // 反向守卫：绝不把横幅的 runId 当内容交出去。
    expect(onStartFreshTurn.mock.calls[0][0]).not.toBe("run_1");
  });

  it("验证未通过（run 仍在飞）：两条出口照旧走 steer / 取消", () => {
    const onSteerRun = vi.fn();
    const onCancelRunningRun = vi.fn();
    const onStartFreshTurn = vi.fn();
    render({
      verificationState: verification,
      onCancelRunningRun,
      onStartFreshTurn,
      onSteerRun,
      runningRunId: "run_1",
    });

    act(() => buttons().find((item) => item.textContent === "带结论重试")!.click());
    act(() => buttons().find((item) => item.textContent === "人工接管")!.click());

    expect(onStartFreshTurn).not.toHaveBeenCalled();
    expect(onSteerRun).toHaveBeenCalledWith("run_1", verificationRetryMessage(verification));
    expect(onCancelRunningRun).toHaveBeenCalledWith("run_1");
  });

  it("验证未通过（run 已终态）且没有「新开一轮」通道：两个出口都不渲染", () => {
    render({
      verificationState: verification,
      onCancelRunningRun: vi.fn(),
      onSteerRun: vi.fn(),
      runningRunId: null,
    });
    const labels = buttons().map((item) => item.textContent);
    expect(labels).not.toContain("带结论重试");
    expect(labels).not.toContain("人工接管");
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

  it("硬停（run 已终态）：「继续」变成「新开一轮」，人工接管不渲染（无对象）", () => {
    const onSteerRun = vi.fn();
    const onCancelRunningRun = vi.fn();
    const onStartFreshTurn = vi.fn();
    // **真实数据流**：runningRunId 为 null（没有在飞 run），而快照里带着
    // willStop=true 的硬停升级——这正是后端特意保留的那一格。
    render({
      escalationState: escalationFromFlow(null),
      onCancelRunningRun,
      onStartFreshTurn,
      onSteerRun,
      runningRunId: null,
    });

    // 终态 run 上不允许再出现一个必然 accepted:false 的"继续"。
    expect(buttons().map((item) => item.textContent)).not.toContain("继续");
    const freshTurn = buttons().find((item) => item.textContent === "新开一轮");
    expect(freshTurn).not.toBeUndefined();

    act(() => freshTurn!.click());

    expect(onSteerRun).not.toHaveBeenCalled();
    // 「继续」这条本来就是单实参调用（`EscalationNotice` 的 willStop 分支），
    // 单实参断言即可证明没有多塞 runId。
    expect(onStartFreshTurn).toHaveBeenCalledTimes(1);
    expect(onStartFreshTurn).toHaveBeenCalledWith(
      "请继续推进，但不要重复已失败的调用；必要时换用其他方法。",
    );
    expect(onStartFreshTurn.mock.calls[0]).toHaveLength(1);
    // 如实说清：运行已结束、没有向它发指令、内容去了输入框（新开一轮）。
    expect(text()).toContain("该运行已安全停止，无法继续");
    expect(text()).toContain("没有向这个运行发任何指令");
  });

  it("硬停但**可复活**（后端判据）：「继续」走同 run 复活，新开一轮与 steer 都不许碰", () => {
    const onSteerRun = vi.fn();
    const onCancelRunningRun = vi.fn();
    const onStartFreshTurn = vi.fn();
    const onResumeRun = vi.fn();
    // **真实数据流**：快照给出 `revivableRunId`（后端算的判据），横幅说的就是这条 run。
    render({
      escalationState: escalationFromFlow(null),
      onCancelRunningRun,
      onResumeRun,
      onStartFreshTurn,
      onSteerRun,
      revivableRunId: "run_1",
      runningRunId: null,
    });

    const resume = buttons().find(
      (item) => item.textContent === "继续（接上这条运行）",
    );
    expect(resume).not.toBeUndefined();
    // 旧的落点不许出现：对这条 run 而言"新开一轮"是错的动作。
    expect(buttons().map((item) => item.textContent)).not.toContain("新开一轮");

    act(() => resume!.click());

    // 关键：**同一条 run id**，且不是"新开一轮"，也不是 steer（必被后端拒）。
    expect(onResumeRun).toHaveBeenCalledTimes(1);
    expect(onResumeRun).toHaveBeenCalledWith("run_1");
    expect(onStartFreshTurn).not.toHaveBeenCalled();
    expect(onSteerRun).not.toHaveBeenCalled();
    // 如实说清：没有新开一轮、停机不计入已用。
    expect(text()).toContain("已在这条运行上接着跑");
    expect(text()).toContain("没有新开一轮");
  });

  it("不可复活的终态：即使给了复活通道也不借用，仍走新开一轮", () => {
    const onStartFreshTurn = vi.fn();
    const onResumeRun = vi.fn();
    render({
      escalationState: escalationFromFlow(null),
      onResumeRun,
      onStartFreshTurn,
      revivableRunId: null,
      runningRunId: null,
    });

    const freshTurn = buttons().find((item) => item.textContent === "新开一轮");
    expect(freshTurn).not.toBeUndefined();
    expect(buttons().map((item) => item.textContent)).not.toContain(
      "继续（接上这条运行）",
    );

    act(() => freshTurn!.click());

    expect(onResumeRun).not.toHaveBeenCalled();
    expect(onStartFreshTurn).toHaveBeenCalledTimes(1);
  });

  it("可复活的是**别的** run：本横幅不许借用那条判据", () => {
    const onResumeRun = vi.fn();
    const onStartFreshTurn = vi.fn();
    render({
      escalationState: escalationFromFlow(null),
      onResumeRun,
      onStartFreshTurn,
      revivableRunId: "run_other",
      runningRunId: null,
    });

    const labels = buttons().map((item) => item.textContent);
    expect(labels).toContain("新开一轮");
    expect(labels).not.toContain("继续（接上这条运行）");
    expect(onResumeRun).not.toHaveBeenCalled();
  });

  it("硬停但**没有**复活通道时：不谎称「继续（接上这条运行）」", () => {
    render({
      escalationState: escalationFromFlow(null),
      onStartFreshTurn: vi.fn(),
      revivableRunId: "run_1",
      runningRunId: null,
    });
    const labels = buttons().map((item) => item.textContent);
    expect(labels).not.toContain("继续（接上这条运行）");
    // 有「新开一轮」通道 ⇒ 落回它（用户还有出路）。
    expect(labels).toContain("新开一轮");
  });

  it("硬停（run 已终态）：「换一条路径」走新开一轮，「人工接管」不渲染（Critical-1）", () => {
    const onSteerRun = vi.fn();
    const onCancelRunningRun = vi.fn();
    const onStartFreshTurn = vi.fn();
    render({
      escalationState: escalationFromEvents(null),
      onCancelRunningRun,
      onStartFreshTurn,
      onSteerRun,
      runningRunId: null,
    });

    const labels = buttons().map((item) => item.textContent);
    // 取消一个已结束的 run 不产生任何可见反馈 ⇒ 不许给这个入口。
    expect(labels).not.toContain("人工接管");
    // 但用户仍要有出路：这条走"新开一轮"，不向已结束的 run 发指令。
    const changeApproach = buttons().find((item) => item.textContent === "换一条路径");
    expect(changeApproach).not.toBeUndefined();

    act(() => changeApproach!.click());

    expect(onCancelRunningRun).not.toHaveBeenCalled();
    expect(onSteerRun).not.toHaveBeenCalled();
    // `EscalationNotice` 对「换一条路径」的调用契约是 `(runId, content)`；
    // `onStartFreshTurn` 是单实参 `(content)`，中间那层适配器**必须丢掉 runId**。
    expect(onStartFreshTurn).toHaveBeenCalledTimes(1);
    expect(onStartFreshTurn).toHaveBeenCalledWith(
      "已失败：read_file(temporary_unavailable×3)。请换一种方法或参数继续，不要原样重复同一个调用。",
    );
    // 反向守卫：绝不把横幅的 runId 当内容交出去（那是"点了做错事"）。
    expect(onStartFreshTurn.mock.calls[0][0]).not.toBe("run_1");
  });

  it("硬停但没有「新开一轮」通道时：谎称「继续」的按钮与「换一条路径」都不给", () => {
    render({
      escalationState: escalationFromFlow(null),
      onCancelRunningRun: vi.fn(),
      onSteerRun: vi.fn(),
      runningRunId: null,
    });
    const labels = buttons().map((item) => item.textContent);
    expect(labels).not.toContain("继续");
    expect(labels).not.toContain("新开一轮");
    expect(labels).not.toContain("换一条路径");
    // 没有对象就不给「人工接管」。
    expect(labels).not.toContain("人工接管");
  });

  it("软阈值（run 仍在飞）：三条出路照旧走 steer / 接管（口径不扩大）", () => {
    const onSteerRun = vi.fn();
    const onCancelRunningRun = vi.fn();
    const onStartFreshTurn = vi.fn();
    render({
      escalationState: escalation,
      onCancelRunningRun,
      onStartFreshTurn,
      onSteerRun,
      runningRunId: "run_1",
    });

    expect(buttons().map((item) => item.textContent)).not.toContain("新开一轮");
    act(() => buttons().find((item) => item.textContent === "继续")!.click());
    act(() => buttons().find((item) => item.textContent === "换一条路径")!.click());
    act(() => buttons().find((item) => item.textContent === "人工接管")!.click());

    expect(onStartFreshTurn).not.toHaveBeenCalled();
    expect(onSteerRun).toHaveBeenCalledWith(
      "run_1",
      "请继续推进，但不要重复已失败的调用；必要时换用其他方法。",
    );
    expect(onSteerRun).toHaveBeenCalledWith(
      "run_1",
      "已失败：read_file(temporary_unavailable×3)。请换一种方法或参数继续，不要原样重复同一个调用。",
    );
    expect(onCancelRunningRun).toHaveBeenCalledWith("run_1");
  });
});
