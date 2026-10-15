import { useState } from "react";
import type { RuntimeV2Escalation } from "./apiTypes";
import {
  ESCALATION_FRESH_TURN_NOTE,
  ESCALATION_OPTION_CHANGE_APPROACH,
  ESCALATION_OPTION_CONTINUE,
  ESCALATION_OPTION_TAKE_OVER,
  ESCALATION_RESUME_NOTE,
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
  /**
   * E3/B：这条硬停 run 是否**可原地复活**。判据来自后端
   * （`snapshot.revivableRunId === state.runId`），前端**不猜**状态。
   */
  revivable?: boolean;
  onContinue?: (runId: string, content: string) => void;
  onChangeApproach?: (runId: string, content: string) => void;
  onTakeOver?: (runId: string) => void;
  /**
   * 切片 10 A2（item 7）：**硬停（已终态）且不可复活**的 run 上"继续"的落点。
   *
   * 终态 run 的 steer 必被后端拒绝（`accepted:false`），所以这个选项改成"新开一轮"：
   * 把要追加的内容交给调用方去开**新的**一轮，不向已结束的 run 发指令。
   * （E3/B 之后：**可复活**的形态不再走这里，走 `onResumeRun`。）
   */
  onStartFreshTurn?: (content: string) => void;
  /**
   * E3/B：硬停且**可复活**时「继续」的落点 —— 调后端在**同一条 run** 上复活。
   * 单实参（runId）：复活**不携带文本**（原样接着跑；想追加新指令的用户走输入框）。
   */
  onResumeRun?: (runId: string) => void;
};

/**
 * C4 终止与升级：无进展/预算将尽时把决定权交回用户。
 *
 * 报告回答三个问题：做到了哪一步（进展）、卡在哪（失败记忆）、能怎么办
 * （继续 / 换路径 / 人工接管）。三个动作都映射到已有的可靠通道。
 *
 * 切片 10 A2（item 7）：**硬停**（`willStop=true`，"已安全停止"）的 run 已经没有
 * 可 steer 的在飞运行，所以"继续"不再走 steer 通道（点了只会得到 `accepted:false`）。
 * E3/B 再把这一格**分流**成两种落点：
 *   * **可复活**（`revivable`，后端判据）⇒ 「继续（接上这条运行）」在**同一条 run** 上
 *     从断点接着跑（run id 不变；停机时间不计入"已用"）；
 *   * **不可复活**（如 FAILED/CANCELLED、该 lane 另有活动 run）⇒ 仍是「新开一轮」，
 *     并如实说明没有向这个运行发任何指令。
 */
export const EscalationNotice = ({
  state,
  pending = false,
  revivable = false,
  onContinue,
  onChangeApproach,
  onTakeOver,
  onStartFreshTurn,
  onResumeRun,
}: EscalationNoticeProps) => {
  const [freshTurnPrepared, setFreshTurnPrepared] = useState(false);
  const [resumed, setResumed] = useState(false);
  const options = state.options.length > 0
    ? state.options
    : [
        ESCALATION_OPTION_CONTINUE,
        ESCALATION_OPTION_CHANGE_APPROACH,
        ESCALATION_OPTION_TAKE_OVER,
      ];
  // E3/B：硬停这一格按**后端判据**分流。"可复活"优先且要求回调在场——
  // 拿不到复活通道时**绝不**谎称「继续（接上这条运行）」（那会让按钮点下去没反应）。
  // 文案必须与**实际落点**一致：`revivable` 但没有复活通道时不能说"接上这条运行"
  // （那会是一个点了做别的事的按钮）。所以先算"这次点击真的会走复活吗"。
  const resumesInPlace = state.willStop && revivable && onResumeRun !== undefined;
  const continueHandler = state.willStop
    ? resumesInPlace
      ? () => {
          onResumeRun(state.runId);
          setResumed(true);
        }
      : onStartFreshTurn
        ? () => {
            onStartFreshTurn(escalationContinueMessage(state));
            setFreshTurnPrepared(true);
          }
        : undefined
    : onContinue
      ? () => onContinue(state.runId, escalationContinueMessage(state))
      : undefined;
  const handlers: Record<string, (() => void) | undefined> = {
    [ESCALATION_OPTION_CONTINUE]: continueHandler,
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
      {state.verdict ? (
        <ul className="escalation-verdict">
          {state.verdict.reasons.map((reason, index) => (
            <li key={`verdict-${index}`}>{reason}</li>
          ))}
          {state.verdict.missing.length > 0 ? (
            <li>缺失：{state.verdict.missing.join("、")}</li>
          ) : null}
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
              {escalationOptionLabel(option, state.willStop, resumesInPlace)}
            </button>
          );
        })}
      </div>
      {resumed ? (
        <p className="escalation-resume-note" role="status">
          {ESCALATION_RESUME_NOTE}
        </p>
      ) : null}
      {freshTurnPrepared ? (
        <p className="escalation-fresh-turn-note" role="status">
          {ESCALATION_FRESH_TURN_NOTE}
        </p>
      ) : null}
    </section>
  );
};
