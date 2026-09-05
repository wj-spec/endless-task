import { useEffect, useState } from "react";
import type {
  CapabilityDelegationStatus,
  RuntimeV2Lane,
  RuntimeV2Metrics,
  RuntimeV2RunUsageCost,
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
} from "../chat/apiTypes";
import {
  fetchRuntimeV2Metrics,
  getRuntimeV2RunUsageCost,
} from "../chat/api";
import type { RuntimeConnectionPhase } from "../chat/runtimeController";
import { StatusBadge } from "../ui/StatusBadge";

type RuntimeStatusPresentation = {
  label: string;
  tone: "neutral" | "active" | "warning" | "danger" | "success";
  pulse: boolean;
};

const runtimeStatusPresentation = (
  status: string | undefined,
): RuntimeStatusPresentation => {
  if (status === "created" || status === "queued") {
    return { label: "准备运行", tone: "active", pulse: true };
  }
  if (status === "running") {
    return { label: "运行中", tone: "active", pulse: true };
  }
  if (status === "waiting_approval") {
    return { label: "等待审批", tone: "warning", pulse: false };
  }
  if (status === "compacting") {
    return { label: "整理上下文", tone: "active", pulse: true };
  }
  if (status === "cancelling") {
    return { label: "正在取消", tone: "warning", pulse: true };
  }
  if (status === "completed") {
    return { label: "已完成", tone: "success", pulse: false };
  }
  if (status === "failed") {
    return { label: "运行失败", tone: "danger", pulse: false };
  }
  if (status === "cancelled") {
    return { label: "已取消", tone: "neutral", pulse: false };
  }
  return { label: "空闲", tone: "neutral", pulse: false };
};

const connectionLabel: Record<RuntimeConnectionPhase, string> = {
  idle: "事件流空闲",
  connecting: "正在连接事件流",
  connected: "事件流已连接",
  reconnecting: "事件流重连中",
};

const entryText: Record<string, string> = {
  user_message: "用户",
  assistant_message: "助手",
  tool_call: "工具调用",
  tool_result: "工具结果",
  context_summary: "上下文摘要",
  plan: "计划",
};

type PlanStep = { title?: string; status?: string };
type PlanPayload = {
  title?: string;
  steps?: PlanStep[];
  currentStepIndex?: number | null;
};

const planStatusLabel: Record<string, string> = {
  pending: "待办",
  in_progress: "进行中",
  completed: "完成",
  skipped: "跳过",
  failed: "失败",
};

function PlanView({ payload }: { payload: PlanPayload | undefined }) {
  const title = payload?.title ?? "计划";
  const steps = payload?.steps ?? [];
  return (
    <div className="plan-card">
      <strong>{title}</strong>
      {steps.length ? (
        <ul>
          {steps.map((step, index) => (
            <li
              className={
                index === payload?.currentStepIndex ? "is-current" : undefined
              }
              key={index}
            >
              <span>{planStatusLabel[step.status ?? "pending"] ?? step.status}</span>
              <span>{step.title}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p>（计划尚未包含步骤）</p>
      )}
    </div>
  );
}

type RuntimeV2PanelProps = {
  connection: {
    phase: RuntimeConnectionPhase;
    error: string | null;
  } | null;
  conversationId: string | null;
  events: RuntimeV2ProductEvent[];
  lanes: RuntimeV2Lane[];
  snapshot: RuntimeV2Snapshot | null;
  delegation?: CapabilityDelegationStatus | null;
};

export function RuntimeV2Panel({
  connection,
  conversationId,
  events,
  lanes,
  snapshot,
  delegation,
}: RuntimeV2PanelProps) {
  const activeRun = snapshot?.runState ?? null;
  const [metrics, setMetrics] = useState<RuntimeV2Metrics | null>(null);
  const [usage, setUsage] = useState<RuntimeV2RunUsageCost | null>(null);
  useEffect(() => {
    let cancelled = false;
    if (!activeRun?.runId) return undefined;
    getRuntimeV2RunUsageCost(activeRun.runId)
      .then((value) => {
        if (!cancelled) setUsage(value);
      })
      .catch(() => {
        /* 诊断用量不可用时保持空白 */
      });
    return () => {
      cancelled = true;
    };
  }, [activeRun?.runId]);
  useEffect(() => {
    let cancelled = false;
    fetchRuntimeV2Metrics()
      .then((value) => {
        if (!cancelled) setMetrics(value);
      })
      .catch(() => {
        /* 指标不可用时保持空白 */
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const presentation = runtimeStatusPresentation(activeRun?.status);
  const selectedLane =
    lanes.find((lane) => lane.id === snapshot?.activeLaneId) ?? null;

  return (
    <section aria-label="执行状态" className="runtime-v2-panel">
      <header className="runtime-v2-header">
        <div>
          <h3>执行状态</h3>
          <p>
            {conversationId
              ? connectionLabel[connection?.phase ?? "idle"]
              : "请先选择会话"}
          </p>
        </div>
        <StatusBadge
          label={presentation.label}
          pulse={presentation.pulse}
          tone={presentation.tone}
        />
      </header>

      <div className="runtime-v2-runtime">
        <div>
          <strong>只读诊断视图</strong>
          <p>执行、审批和恢复操作请在对应聊天工作面完成。</p>
        </div>
      </div>

      {delegation ? (
        <div className="runtime-v2-runtime">
          <div>
            <strong>子代理委派（M4A 只读）</strong>
            <p>
              {delegation.enabled
                ? `已启用（mode=${delegation.mode ?? "readonly"}）：可通过 spawn_agent 派只读研究子代理`
                : "已关闭：不注册委派工具，agent 无法派生子代理"}
            </p>
          </div>
        </div>
      ) : null}

      {connection?.error ? (
        <div className="inline-error" role="alert">
          <span>{connection.error}</span>
        </div>
      ) : null}

      {snapshot ? (
        <>
          <div className="runtime-v2-metrics">
            <span>事件 {snapshot.lastEventSeq}</span>
            <span>
              Tokens {snapshot.contextUsage.inputTokens}/
              {snapshot.contextUsage.outputTokens}
            </span>
            <span>工具 {snapshot.toolStates.length}</span>
            {usage ? (
              <span>
                成本 ${usage.totals.costUsd.toFixed(4)} · 用量{" "}
                {usage.totals.inputTokens}/{usage.totals.outputTokens} tokens
              </span>
            ) : null}
          </div>

          {metrics ? (
            <div className="runtime-v2-card">
              <strong>运行时指标（本进程）</strong>
              <p>
                Runs {metrics.runs.count} · 平均首 token{" "}
                {metrics.modelTurns.avgFirstTokenLatencyMs ?? "—"}ms · 压缩{" "}
                {metrics.compactions.events} 次
              </p>
              <p>
                前缀稳定率{" "}
                {metrics.prefixStability.stableRate == null
                  ? "—"
                  : `${Math.round(metrics.prefixStability.stableRate * 100)}%`}{" "}
                · 审批 {metrics.approvals.count} 次
              </p>
            </div>
          ) : null}

          <div className="runtime-v2-lanes">
            <div className="runtime-v2-lane-toolbar">
              <strong>Lanes</strong>
              <span>
                主线 {snapshot.mainLaneId ?? "—"} · 运行位置{" "}
                {snapshot.runningLaneId ?? "—"}
              </span>
            </div>
            {lanes.map((lane) => (
              <div
                className={
                  lane.id === selectedLane?.id
                    ? "runtime-v2-lane is-selected"
                    : "runtime-v2-lane"
                }
                key={lane.id}
              >
                <div>
                  <span>
                    {lane.isMain
                      ? "主线"
                      : lane.archived
                        ? "已归档"
                        : lane.kind === "temporary"
                          ? "临时"
                          : "分支"}
                  </span>
                  <strong>{lane.displayName ?? lane.title ?? lane.id}</strong>
                </div>
              </div>
            ))}
          </div>

          {activeRun ? (
            <div className="runtime-v2-card">
              <strong>Run {activeRun.runId}</strong>
              <p>
                状态 {activeRun.status} · 当前 Lane {snapshot.activeLaneId ?? "—"}
              </p>
              {activeRun.partialContent ? <p>{activeRun.partialContent}</p> : null}
            </div>
          ) : (
            <p className="runtime-v2-empty">当前 Lane 没有 Run。</p>
          )}

          {snapshot.pendingApprovals.map((approval) => (
            <div className="runtime-v2-approval" key={approval.id}>
              <strong>{approval.summary}</strong>
              <p>
                {approval.toolName} · {approval.reason}
              </p>
            </div>
          ))}

          {snapshot.interruptedRuns.map((report) => (
            <div className="runtime-v2-recovery" key={report.runId}>
              <strong>需恢复：{report.runId}</strong>
              <p>
                {report.classification} · 建议动作 {report.action}
              </p>
              <ul>
                {report.findings.map((finding, index) => (
                  <li key={`${finding.reason}:${index}`}>{finding.message}</li>
                ))}
              </ul>
            </div>
          ))}

          {snapshot.toolStates.length ? (
            <div className="runtime-v2-turns">
              {snapshot.toolStates.map((tool) => (
                <div key={tool.id}>
                  <strong>{tool.toolName}</strong>
                  <span>{tool.status}</span>
                </div>
              ))}
            </div>
          ) : null}

          <div className="runtime-v2-entries">
            {snapshot.entries.length ? (
              snapshot.entries.map((entry) => (
                <div key={entry.id}>
                  <span>{entryText[entry.type] ?? entry.type}</span>
                  {entry.type === "plan" ? (
                    <PlanView
                      payload={
                        entry.data.reference as PlanPayload | undefined
                      }
                    />
                  ) : (
                    <p>
                      {entry.data.content ??
                        entry.data.toolName ??
                        entry.data.errorCode ??
                        (entry.data.reference as
                          | { content?: string }
                          | undefined)?.content ??
                        entry.id}
                    </p>
                  )}
                </div>
              ))
            ) : (
              <p className="runtime-v2-empty">当前 Lane 尚无 transcript。</p>
            )}
          </div>

          <div className="runtime-v2-events">
            {events.slice(-40).map((event) => (
              <div key={event.eventId}>
                <strong>
                  #{event.eventSeq} {event.type}
                </strong>
                <span>
                  {event.laneId ?? "conversation"} · {event.runId ?? "—"}
                </span>
              </div>
            ))}
          </div>
        </>
      ) : (
        <p className="runtime-v2-empty">
          {conversationId ? "正在等待共享 Runtime 快照。" : "请选择一个会话。"}
        </p>
      )}
    </section>
  );
}