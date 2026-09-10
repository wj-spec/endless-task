import type { RuntimeV2RecoveryReport, RuntimeV2Snapshot } from "./apiTypes";
import type { RuntimeConnectionPhase } from "./runtimeController";

/**
 * 运行恢复提示区：SSE 重连中的提示 + 中断运行的恢复卡片。
 *
 * 从 `ChatWorkSurface.tsx`（1 541 行）搬出。搬出前这段"什么时候出现、能点哪几个按钮"
 * 完全依赖 `recoveryPresentation` 的分类表，而那张表散在 1 500 行组件的中间；搬出后
 * 分类表与渲染在同一个文件里，也能按 `findings` 组合直测（e2e 只覆盖了一两种）。
 *
 * 行为零改动：`classification`/`action`/`findings` 的判定顺序、文案、`role` 与
 * `aria-*`、按钮禁用条件（`pending`）逐字保持原样。
 */
export function recoveryPresentation(report: RuntimeV2RecoveryReport) {
  const reasons = new Set(report.findings.map((finding) => finding.reason));
  if (
    reasons.has("waiting_approval") ||
    reasons.has("tool_side_effect_uncertain") ||
    reasons.has("tool_result_missing")
  ) {
    return {
      title: "上次操作需要确认",
      message: "应用中断时可能正在执行工具。请先检查相关结果，再结束这次运行。",
      canRetry: false,
    };
  }
  if (reasons.has("cancellation_pending")) {
    return {
      title: "上次停止操作未完成",
      message: "这是一次停止动作留下的中间态；可以重新生成，或直接结束本次运行。",
      canRetry: false,
    };
  }
  if (report.classification === "recoverable" && report.action === "resume") {
    return {
      title: "上次运行被中断",
      message: "连接恢复后可以接着跑；也可以直接结束这次运行。",
      canRetry: true,
    };
  }
  // 兜底分支：能走到这里说明 classification/action 不同时满足 recoverable+resume
  // （上面那条已把该组合提前返回），所以下面的 `canRetry` 表达式恒为 false。
  // 保留原表达式是**行为零改动**的要求（搬迁时不做"顺手简化"）；产品口径由
  // RuntimeRecoveryNotices.test.tsx 里的穷举用例钉住：只有 recoverable+resume 允许重试。
  return {
    title: "上次运行未能完成",
    message: "重新生成会在同一轮里重试，不会丢失已有上下文。",
    canRetry:
      report.classification === "recoverable" && report.action === "resume",
  };
}

export type RuntimeRecoveryNoticesProps = {
  connectionPhase?: RuntimeConnectionPhase;
  runtimeSnapshot?: RuntimeV2Snapshot | null;
  pending: boolean;
  onResolveRuntimeRecovery?: (
    runId: string,
    action: "retry" | "mark_failed",
  ) => void;
};

export function RuntimeRecoveryNotices({
  connectionPhase,
  runtimeSnapshot,
  pending,
  onResolveRuntimeRecovery,
}: RuntimeRecoveryNoticesProps) {
  return (
    <>
      {connectionPhase === "reconnecting" ? (
        <section
          aria-atomic="true"
          aria-live="polite"
          className="runtime-recovery-card is-connecting"
          role="status"
        >
          <strong>正在恢复连接</strong>
          <p>现有运行仍被保留，连接恢复前不会重复提交消息。</p>
        </section>
      ) : null}

      {onResolveRuntimeRecovery
        ? runtimeSnapshot?.interruptedRuns.map((report) => {
            const presentation = recoveryPresentation(report);
            return (
              <section
                className="runtime-recovery-card"
                key={report.runId}
                role="alert"
              >
                <strong>{presentation.title}</strong>
                <p>{presentation.message}</p>
                <div className="runtime-recovery-actions">
                  {presentation.canRetry ? (
                    <button
                      disabled={pending}
                      onClick={() => onResolveRuntimeRecovery(report.runId, "retry")}
                      type="button"
                    >
                      重新生成
                    </button>
                  ) : null}
                  <button
                    disabled={pending}
                    onClick={() =>
                      onResolveRuntimeRecovery(report.runId, "mark_failed")
                    }
                    type="button"
                  >
                    结束本次运行
                  </button>
                </div>
              </section>
            );
          })
        : null}
    </>
  );
}
