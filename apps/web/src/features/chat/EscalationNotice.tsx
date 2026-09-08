import type { RuntimeV2Escalation } from "./apiTypes";
import {
  ESCALATION_OPTION_CHANGE_APPROACH,
  ESCALATION_OPTION_CONTINUE,
  ESCALATION_OPTION_TAKE_OVER,
  escalationContinueMessage,
  escalationHeadline,
  escalationOptionLabel,
  escalationProgressText,
  escalationReasonLabel,
} from "./escalation";
import { repeatedFailureText, stuckSteerMessage } from "./stuckState";

type EscalationNoticeProps = {
  state: RuntimeV2Escalation;
  pending?: boolean;
  onContinue?: (runId: string, content: string) => void;
  onChangeApproach?: (runId: string, content: string) => void;
  onTakeOver?: (runId: string) => void;
};

/**
 * C4 终止与升级：无进展/预算将尽时把决定权交回用户。
 *
 * 报告回答三个问题：做到了哪一步（进展）、卡在哪（失败记忆）、能怎么办
 * （继续 / 换路径 / 人工接管）。三个动作都映射到已有的可靠通道。
 */
export const EscalationNotice = ({
  state,
  pending = false,
  onContinue,
  onChangeApproach,
  onTakeOver,
}: EscalationNoticeProps) => {
  const options = state.options.length > 0
    ? state.options
    : [
        ESCALATION_OPTION_CONTINUE,
        ESCALATION_OPTION_CHANGE_APPROACH,
        ESCALATION_OPTION_TAKE_OVER,
      ];
  const handlers: Record<string, (() => void) | undefined> = {
    [ESCALATION_OPTION_CONTINUE]: onContinue
      ? () => onContinue(state.runId, escalationContinueMessage(state))
      : undefined,
    [ESCALATION_OPTION_CHANGE_APPROACH]: onChangeApproach
      ? () => onChangeApproach(state.runId, stuckSteerMessage(state))
      : undefined,
    [ESCALATION_OPTION_TAKE_OVER]: onTakeOver
      ? () => onTakeOver(state.runId)
      : undefined,
  };
  return (
    <section
      aria-label="需要你决定下一步"
      className="inline-notice escalation-notice"
      role="status"
    >
      <header className="escalation-header">
        <strong>{escalationHeadline(state)}</strong>
        <span className="escalation-reason-badge">
          {escalationReasonLabel(state.reason)}
        </span>
      </header>
      <p className="escalation-summary">{state.summary}</p>
      <p className="escalation-progress">{escalationProgressText(state)}</p>
      {state.repeatedFailures.length > 0 ? (
        <ul className="escalation-failures">
          {state.repeatedFailures.map((item) => (
            <li key={`${item.toolName}-${item.errorCode}`}>
              {repeatedFailureText(item)}
            </li>
          ))}
        </ul>
      ) : null}
      {state.guidance ? (
        <p className="escalation-guidance">{state.guidance}</p>
      ) : null}
      <div className="escalation-actions">
        {options.map((option) => {
          const handler = handlers[option];
          if (!handler) return null;
          return (
            <button
              disabled={pending}
              key={option}
              onClick={handler}
              type="button"
            >
              {escalationOptionLabel(option)}
            </button>
          );
        })}
      </div>
    </section>
  );
};
