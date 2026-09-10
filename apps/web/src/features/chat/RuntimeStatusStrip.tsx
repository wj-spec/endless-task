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
 * `runtimeSnapshot?.activeRunId ?? runningRunId`、`pendingAction !== null` 这些表达式
 * 提前算好作为 props 传入（与原判断等价）。
 */
export type RuntimeStatusStripProps = {
  usage?: RuntimeV2UsageSummary | null;
  /** 传给审计面板的刷新键（原 `runtimeSnapshot?.lastEventSeq ?? null`）。 */
  lastEventSeq: number | null;
  /** 审计面板要展示的运行 id；没有活动/运行中运行时为 null。 */
  activeRunId: string | null;
  verificationState?: RuntimeV2Verification | null;
  escalationState?: RuntimeV2Escalation | null;
  stuckState?: RuntimeV2StuckState | null;
  pending: boolean;
  onSteerRun?: (runId: string, content: string) => void;
  onCancelRunningRun?: (runId: string) => void;
};

export function RuntimeStatusStrip({
  usage,
  lastEventSeq,
  activeRunId,
  verificationState,
  escalationState,
  stuckState,
  pending,
  onSteerRun,
  onCancelRunningRun,
}: RuntimeStatusStripProps) {
  return (
    <>
      {usage ? <UsageMeter usage={usage} /> : null}

      <AuditTrailPanel
        refreshKey={lastEventSeq}
        runId={activeRunId}
      />

      {verificationState ? (
        <VerificationBadge
          onRetry={onSteerRun}
          onTakeOver={onCancelRunningRun}
          pending={pending}
          state={verificationState}
        />
      ) : null}

      {escalationState ? (
        <EscalationNotice
          onChangeApproach={onSteerRun}
          onContinue={onSteerRun}
          onTakeOver={onCancelRunningRun}
          pending={pending}
          state={escalationState}
        />
      ) : stuckState ? (
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
