import type {
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  RuntimeV2Verification,
} from "./apiTypes";

/**
 * C1 制造者—检查者分离：独立验证状态的折叠与文案。
 *
 * 后端在制造者产出后派发独立验证（`run.verifying`），拿到结构化判定后发
 * `run.verified`。前端只消费这两类事件 + 快照，不参与判定。
 */
const asString = (value: unknown, fallback = ""): string =>
  typeof value === "string" && value.length > 0 ? value : fallback;

const asStringArray = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

export const verificationFromPayload = (
  payload: Record<string, unknown>,
  runId: string,
  status: string,
): RuntimeV2Verification => ({
  runId: asString(payload.runId, runId),
  status,
  verdict:
    typeof payload.verdict === "string" && payload.verdict.length > 0
      ? payload.verdict
      : null,
  reasons: asStringArray(payload.reasons),
  missing: asStringArray(payload.missing),
  model: asString(payload.model),
  latencyMs: typeof payload.latencyMs === "number" ? payload.latencyMs : null,
  inputTokens:
    typeof payload.inputTokens === "number" ? payload.inputTokens : null,
  outputTokens:
    typeof payload.outputTokens === "number" ? payload.outputTokens : null,
});

export const deriveVerificationState = (
  snapshot: RuntimeV2Snapshot | null | undefined,
  events: RuntimeV2ProductEvent[] | undefined,
): RuntimeV2Verification | null => {
  let current = snapshot?.verification ?? null;
  for (const event of events ?? []) {
    const payload = (event.data ?? {}) as Record<string, unknown>;
    if (event.type === "run.verifying") {
      current = {
        ...verificationFromPayload(payload, event.runId ?? "", "verifying"),
        status: "verifying",
        verdict: null,
      };
      continue;
    }
    if (event.type === "run.verified") {
      current = verificationFromPayload(payload, event.runId ?? "", "verified");
    }
  }
  return current;
};

export const verificationBadgeText = (state: RuntimeV2Verification): string => {
  if (state.status === "verifying" || state.verdict === null) {
    return "独立验证中…";
  }
  if (state.verdict === "pass") return "独立验证：通过";
  if (state.verdict === "fail") return "独立验证：未通过";
  return "独立验证：不确定";
};

export const verificationDetailText = (
  state: RuntimeV2Verification,
): string => {
  const reasons = state.reasons.slice(0, 2).join("；");
  const missing = state.missing.length > 0
    ? `缺失：${state.missing.slice(0, 2).join("、")}`
    : "";
  return [reasons, missing].filter((item) => item.length > 0).join("；");
};

/** 验证未通过时"带结论重试"要注入的指令。 */
export const verificationRetryMessage = (
  state: RuntimeV2Verification,
): string => {
  const reasons = state.reasons.join("；");
  const missing = state.missing.join("、");
  const detail = [
    reasons ? `验证者指出：${reasons}` : "",
    missing ? `缺失：${missing}` : "",
  ]
    .filter((item) => item.length > 0)
    .join("；");
  return detail
    ? `独立验证未通过。${detail}。请针对这些问题修正后重新完成。`
    : "独立验证未通过，请检查产出是否真的完成了目标，并修正后重新完成。";
};
