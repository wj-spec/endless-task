import type { RuntimeV2StuckState } from "./apiTypes";
import { repeatedFailureText, stuckHeadline, stuckSteerMessage } from "./stuckState";

type StuckNoticeProps = {
  state: RuntimeV2StuckState;
  pending?: boolean;
  onSteer?: (runId: string, content: string) => void;
  onTakeOver?: (runId: string) => void;
};

/**
 * C2 失败记忆：循环卡住时的可见外部状态。
 *
 * 展示"试过什么、为什么失败、第几次"，并给出两条出路：换一条路径
 * （把纠偏指令注入运行中的 steer 通道）或人工接管（停止本次运行）。
 *
 * 为什么这里**不**做"该 run 是否还在飞"的分支：卡住态本身只在在飞 run 上存在
 * （后端 `_stuck_json` 在 `running_run_id is None` 时直接返回 None；前端
 * `deriveStuckState` 见到同一 run 的终态事件即清空横幅），所以卡住横幅**不会**挂在
 * 终态 run 上。真正会在终态存活的是 `willStop=true` 的硬停升级横幅——那条分支由
 * 调用方 `RuntimeStatusStrip` 用真实在飞信号 `runningRunId` 做（见那里的注释）。
 */
export const StuckNotice = ({
  state,
  pending = false,
  onSteer,
  onTakeOver,
}: StuckNoticeProps) => {
  const reasons =
    state.reasons.length > 0
      ? state.reasons
      : state.repeatedFailures.map(repeatedFailureText);
  return (
    <div
      aria-label="运行卡住"
      className={`inline-notice stuck-notice stuck-notice-${state.level}`}
      role="status"
    >
      <strong>{stuckHeadline(state)}</strong>
      <span className="stuck-summary">
        已记录 {state.attempts.length || state.repeatedFailures.length} 次失败尝试
      </span>
      {reasons.length > 0 ? (
        <ul className="stuck-reasons">
          {reasons.map((reason, index) => (
            <li key={`${reason}-${index}`}>{reason}</li>
          ))}
        </ul>
      ) : null}
      {state.guidance ? (
        <p className="stuck-guidance">{state.guidance}</p>
      ) : null}
      <div className="stuck-actions">
        {onSteer ? (
          <button
            disabled={pending}
            onClick={() => onSteer(state.runId, stuckSteerMessage(state))}
            type="button"
          >
            换一条路径
          </button>
        ) : null}
        {onTakeOver ? (
          <button
            disabled={pending}
            onClick={() => onTakeOver(state.runId)}
            type="button"
          >
            人工接管
          </button>
        ) : null}
      </div>
    </div>
  );
};
