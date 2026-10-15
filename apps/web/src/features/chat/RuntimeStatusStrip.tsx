import type {
  RuntimeV2Escalation,
  RuntimeV2StuckState,
  RuntimeV2UsageSummary,
  RuntimeV2Verification,
} from "./apiTypes";
import { AuditTrailPanel } from "./AuditTrailPanel";
import { EscalationNotice } from "./EscalationNotice";
import { StuckNotice } from "./StuckNotice";
import { UsageMeter } from "./UsageMeter";
import { VerificationBadge } from "./VerificationBadge";

/**
 * 运行状态条：用量、审计轨迹、验证结论、升级/卡住提示。
 *
 * 从 `ChatWorkSurface.tsx`（1 459 行）搬出。这四块此前挤在消息流上方的一段 JSX 里，
 * "哪些会同时出现、点哪个按钮做什么"很难从 1 500 行组件里读出来；搬出后可以按状态
 * 组合直测，并把"升级与卡住二选一"这条规则钉住（`escalationState` 优先，否则
 * 退到 `stuckState`）。
 *
 * 行为零改动：渲染顺序、组件选择与传参逐字保持；只把 `runtimeSnapshot?.lastEventSeq`、
 * `runtimeSnapshot?.activeRunId`、`runtimeSnapshot?.runningRunId`、
 * `pendingAction !== null` 这些表达式提前算好作为 props 传入（与原判断等价）。
 *
 * **除一处**（切片 10 A2 item 7 的收口，批次 C0 复审 Critical-1 修订）：**升级**与
 * **验证**横幅的出口按"该 run 是否**真的**还在飞"分支。
 *
 * 为什么这两块需要而卡住横幅不需要：升级与验证都以 `active_run_id` 兜底投影，而
 * `active_run_id` 是"当前 lane 的活动变体"指针、run 终态后**依然**指向它
 * （`replay.py` 的 `lane_runs[-1]` 没有任何 status 过滤）。具体地：
 *   - `willStop=true` 的硬停升级被后端**特意**保留到终态之后
 *     （`_escalation_json`：`if terminal_run and not escalation["willStop"]: return None`）；
 *   - `verification` 由 `_verification_json(run_id=active_run_id or running_run_id)`
 *     投影，前端 `deriveVerificationState` 也**没有**终态清理分支 ⇒ 徽标同样能活到终态。
 * 反过来，卡住横幅不可能处于终态（`_stuck_json` 在 `running_run_id is None` 时直接
 * 返回 None），所以它**不加**判据。
 *
 * 判定必须用**真实在飞信号** `runningRunId`（后端 `replay.py` 只把非终态 run 放进去，
 * 空转时为 null），**不能**用 `activeRunId`：那是上面说的那个指针，拿它判会恒真。
 */
export type RuntimeStatusStripProps = {
  usage?: RuntimeV2UsageSummary | null;
  /** 传给审计面板的刷新键（原 `runtimeSnapshot?.lastEventSeq ?? null`）。 */
  lastEventSeq: number | null;
  /** 审计面板要展示的运行 id；没有活动/运行中运行时为 null。 */
  activeRunId: string | null;
  /**
   * 真实在飞信号（原 `runtimeSnapshot?.runningRunId ?? null`）：只有仍在
   * created/queued/running/waiting_approval/compacting/cancelling 的 run 才有值，
   * 空转时为 null。**不是**"当前 lane 的活动变体"。
   */
  runningRunId: string | null;
  /**
   * E3/B：后端判定「这条硬停 run 可以**原地复活**」时给出它的 id（否则 null）。
   * 与 `runningRunId` 同一种姿势——**真实判据来自后端快照**，前端不猜状态。
   */
  revivableRunId?: string | null;
  verificationState?: RuntimeV2Verification | null;
  escalationState?: RuntimeV2Escalation | null;
  stuckState?: RuntimeV2StuckState | null;
  pending: boolean;
  onSteerRun?: (runId: string, content: string) => void;
  onCancelRunningRun?: (runId: string) => void;
  /**
   * 切片 10 A2（item 7）：终态（= 没有在飞 run）时横幅里"继续推进 / 换一条路径 /
   * 带结论重试"的落点——开**新的**一轮。终态 run 的 steer 必被后端拒绝，所以这条
   * 通道不与 `onSteerRun` 复用。
   */
  onStartFreshTurn?: (content: string) => void;
  /**
   * E3/B：硬停且**可复活**时「继续」的落点——在**同一条 run** 上从断点接着跑。
   * 与 `onStartFreshTurn`（新开一轮）是**分流**关系，判据是 `revivableRunId`。
   */
  onResumeRun?: (runId: string) => void;
};

export function RuntimeStatusStrip({
  usage,
  lastEventSeq,
  activeRunId,
  runningRunId,
  revivableRunId = null,
  verificationState,
  escalationState,
  stuckState,
  pending,
  onSteerRun,
  onCancelRunningRun,
  onStartFreshTurn,
  onResumeRun,
}: RuntimeStatusStripProps) {
  // 这条横幅说的 run 是不是**真的**还在飞。为 false 时它已经终态（横幅会一直挂到
  // "下一个 run 开始"或用户处理），此时：
  //   - 「人工接管」（= 取消那条 run）没有任何对象，取消一个已结束的 run 不产生
  //     任何可见反馈（`cancelRuntimeRun` 成功分支只静默刷新快照）⇒ 不渲染；
  //   - 其余指令型出口（「换一条路径」/「带结论重试」）改走 `onStartFreshTurn`
  //     （与「继续」同落点）⇒ 不向已结束的 run 发必然被拒的指令。
  // 为 true（软阈值 / 验证进行中那种真在跑的 run）时出口照旧。
  const escalationRunActive =
    runningRunId !== null &&
    escalationState !== null &&
    escalationState !== undefined &&
    escalationState.runId === runningRunId;

  /**
   * E3/B：这条硬停横幅说的 run 能不能**原地复活**。判据是后端给的
   * `revivableRunId`（不是前端从状态推的），并且必须**同一条 run**——
   * 同会话里别的 lane 的横幅不许借用这条判据。
   */
  const escalationRevivable =
    revivableRunId !== null &&
    escalationState !== null &&
    escalationState !== undefined &&
    escalationState.runId === revivableRunId;

  // 同形判据：`verification` 也以 `active_run_id` 兜底投影，所以 `verdict: "fail"`
  // 的徽标同样能活到 run 终态之后。
  const verificationRunActive =
    runningRunId !== null &&
    verificationState !== null &&
    verificationState !== undefined &&
    verificationState.runId === runningRunId;

  /**
   * 终态时把 `(runId, content)` 契约的回调适配到单实参的 `onStartFreshTurn`：
   * `VerificationBadge` / `EscalationNotice` 都按 `(state.runId, message)` 调用，
   * 而落点 `ChatWorkSurface` 的 `onStartFreshTurn` 只取 `content`——这里必须**丢掉
   * 第一个实参**，否则会把 runId 当内容写进草稿。
   */
  const freshTurnFromRunScoped =
    onStartFreshTurn === undefined
      ? undefined
      : (_runId: string, content: string) => onStartFreshTurn(content);

  return (
    <>
      {usage ? <UsageMeter usage={usage} /> : null}

      <AuditTrailPanel
        refreshKey={lastEventSeq}
        runId={activeRunId}
      />

      {verificationState ? (
        <VerificationBadge
          onRetry={verificationRunActive ? onSteerRun : freshTurnFromRunScoped}
          onTakeOver={verificationRunActive ? onCancelRunningRun : undefined}
          pending={pending}
          state={verificationState}
        />
      ) : null}

      {escalationState ? (
        <EscalationNotice
          onChangeApproach={
            escalationRunActive ? onSteerRun : freshTurnFromRunScoped
          }
          onContinue={onSteerRun}
          onResumeRun={escalationRevivable ? onResumeRun : undefined}
          onStartFreshTurn={onStartFreshTurn}
          onTakeOver={escalationRunActive ? onCancelRunningRun : undefined}
          pending={pending}
          revivable={escalationRevivable}
          state={escalationState}
        />
      ) : stuckState ? (
        // 卡住横幅不会挂在终态 run 上（见文件头注释），所以照旧无条件传两条出路：
        // 这样"用户在卡住的 run 还在跑时切走 lane"也不会把出口误藏起来
        // （`runningRunId` 按会话取、`activeRunId` 按所选 lane 取，两者可以不同）。
        <StuckNotice
          onSteer={onSteerRun}
          onTakeOver={onCancelRunningRun}
          pending={pending}
          state={stuckState}
        />
      ) : null}
    </>
  );
}
