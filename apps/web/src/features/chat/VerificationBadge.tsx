import type { RuntimeV2Verification } from "./apiTypes";
import {
  verificationBadgeText,
  verificationDetailText,
  verificationRetryMessage,
} from "./verification";

type VerificationBadgeProps = {
  state: RuntimeV2Verification;
  pending?: boolean;
  onRetry?: (runId: string, content: string) => void;
  onTakeOver?: (runId: string) => void;
};

/**
 * C1 制造者—检查者分离：把"独立验证"的结论摆在产出旁边。
 *
 * 验证由独立上下文/prompt 完成，所以这里展示的是"别人的判断"，不是模型自评；
 * 未通过时提供「带结论重试」与「人工接管」。
 */
export const VerificationBadge = ({
  state,
  pending = false,
  onRetry,
  onTakeOver,
}: VerificationBadgeProps) => {
  const detail = verificationDetailText(state);
  const failed = state.verdict === "fail";
  return (
    <div
      aria-label="独立验证结果"
      className={`verification-badge verification-${state.verdict ?? state.status}`}
      role="status"
    >
      <span className="verification-badge-text">{verificationBadgeText(state)}</span>
      {state.model ? (
        <span className="verification-badge-model">{state.model}</span>
      ) : null}
      {detail ? <span className="verification-badge-detail">{detail}</span> : null}
      {failed && onRetry ? (
        <button
          disabled={pending}
          onClick={() => onRetry(state.runId, verificationRetryMessage(state))}
          type="button"
        >
          带结论重试
        </button>
      ) : null}
      {failed && onTakeOver ? (
        <button
          disabled={pending}
          onClick={() => onTakeOver(state.runId)}
          type="button"
        >
          人工接管
        </button>
      ) : null}
    </div>
  );
};
