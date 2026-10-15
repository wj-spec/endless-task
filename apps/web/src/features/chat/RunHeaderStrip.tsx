import { useEffect, useState } from "react";
import type { RuntimeV2RunState, RuntimeV2UsageSummary } from "./apiTypes";
import type { RuntimeConnectionPhase } from "./runtimeController";
import { formatCost, formatTokens } from "./usageCost";
import {
  connectionPhaseLabel,
  formatElapsedCompact,
  isRunActive,
  runBudgetText,
  runElapsedSeconds,
  runStatusLabel,
  runStepText,
} from "./runHeader";

/**
 * 切片 10 T4（item 1）：run 头部条。
 *
 * 长活（几十分钟到几小时的 run）最难回答的三件事——"跑了多久了""现在在干什么"
 * "花了多少"——此前散在消息流里：elapsed 只有升级横幅里才有、当前工具靠轨迹面板、
 * 成本在 `UsageMeter` 里。这条常驻在会话工作区顶部，把四件事收在一行：
 *
 * ① 活时钟 elapsed（客户端从 `runState.startedAt` 连续起算，不依赖事件刷新；
 *    终态改为展示后端冻结的 `elapsedSeconds`，停止后时钟必须停）；
 * ② 当前工具/步骤（`runState.currentStep`：在飞工具优先，否则最后跑过的那个）；
 * ③ token/成本（快照 `usage`）+ 执行器声明的预算墙（`runState.budget`）；
 * ④ 连接相位（复用 `runtimeController` 的 connected/reconnecting/... 四档）。
 *
 * 后端没给的一律显示"—"或"尚未开始"，不用"当前时间"伪装起点、不退回默认预算档。
 * 不新增按钮，因此没有键盘可达性回归；重连的第二遍播报仍由 `RuntimeRecoveryNotices`
 * 负责，这里刻意**不加** `aria-live`，否则每秒的时钟会持续打断读屏。
 */
export type RunHeaderStripProps = {
  /** 本泳道当前那次 run 的状态；null = 没有可讲的 run，整条不渲染。 */
  runState: RuntimeV2RunState | null;
  /** 该 run 的用量/成本（快照 `usage`）；null = 还没统计出来，显示"—"。 */
  usage?: RuntimeV2UsageSummary | null;
  connectionPhase?: RuntimeConnectionPhase;
  /** 时钟注入（测试用）；默认 `Date.now`。 */
  now?: () => number;
};

const TICK_MS = 1000;

const defaultNow = () => Date.now();

export function RunHeaderStrip({
  runState,
  usage = null,
  connectionPhase,
  now,
}: RunHeaderStripProps) {
  const nowFn = now ?? defaultNow;
  const active = runState ? isRunActive(runState.status) : false;
  const runId = runState?.runId ?? null;
  const [nowMs, setNowMs] = useState(() => nowFn());

  // 依赖刻意只列 `active`/`nowFn`/`runId`：`runState` 对象每次快照刷新（运行中约 220ms
  // 一次）都是新引用，按它做依赖会不停重建计时器；真正需要重建的只有"换 run"与
  // "跑完/开跑"这两个跃迁。
  useEffect(() => {
    if (runId === null) return;
    setNowMs(nowFn());
    // 只有还在跑才挂计时器：终态的时钟不再走，省掉一个永久 1s 定时器。
    if (!active) return;
    const timer = globalThis.setInterval(() => setNowMs(nowFn()), TICK_MS);
    return () => globalThis.clearInterval(timer);
  }, [active, nowFn, runId]);

  if (!runState) return null;

  const elapsed = runElapsedSeconds(runState, nowMs);
  const stepText = runStepText(runState);
  const budgetText = runBudgetText(runState.budget ?? null);
  const usageText = usage
    ? `${formatTokens(usage.totalTokens)} tokens · ${formatCost(usage.costUsd, usage.costPriced)}`
    : "—";

  return (
    <section
      aria-label="本次运行状态"
      className={`run-header${active ? " is-running" : ""}`}
      data-phase={connectionPhase ?? "unknown"}
      data-run-status={runState.status}
    >
      <span className="run-header-item run-header-status">
        <span className="run-header-label">状态</span>
        <span className="run-header-value">{runStatusLabel(runState.status)}</span>
      </span>
      <span
        className="run-header-item run-header-elapsed"
        title={
          elapsed === null
            ? "后端没有给出这次运行的起点，无法计算已用时。"
            : "已用时按运行起点（后端 run.startedAt）本地连续计时。"
        }
      >
        <span className="run-header-label">已运行</span>
        <span className="run-header-value">{formatElapsedCompact(elapsed)}</span>
      </span>
      <span className="run-header-item run-header-step">
        <span className="run-header-label">当前</span>
        <span className="run-header-value">{stepText ?? "尚未开始"}</span>
      </span>
      <span className="run-header-item run-header-usage">
        <span className="run-header-label">用量</span>
        <span className="run-header-value">{usageText}</span>
      </span>
      {budgetText ? (
        <span className="run-header-item run-header-budget">
          <span className="run-header-label">预算</span>
          <span className="run-header-value">{budgetText}</span>
        </span>
      ) : null}
      <span className="run-header-item run-header-phase">
        <span className="run-header-label">连接</span>
        <span className="run-header-value">
          {connectionPhaseLabel(connectionPhase)}
        </span>
      </span>
    </section>
  );
}
