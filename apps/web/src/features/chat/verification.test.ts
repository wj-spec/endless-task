import { describe, expect, it } from "vitest";
import type {
  RuntimeV2ProductEvent,
  RuntimeV2Snapshot,
  RuntimeV2Verification,
} from "./apiTypes";
import {
  deriveVerificationState,
  verificationBadgeText,
  verificationDetailText,
  verificationRetryMessage,
} from "./verification";

const event = (
  type: string,
  data: Record<string, unknown> = {},
  runId = "run_1",
): RuntimeV2ProductEvent =>
  ({
    eventId: `evt_${type}`,
    eventSeq: 1,
    type,
    conversationId: "conv_1",
    laneId: "lane_1",
    runId,
    createdAt: "2026-01-01T00:00:00Z",
    data: { runId, ...data },
  }) as RuntimeV2ProductEvent;

const snapshotWith = (verification: RuntimeV2Verification | null) =>
  ({ verification } as unknown as RuntimeV2Snapshot);

describe("deriveVerificationState（C1 独立验证）", () => {
  it("无快照无事件时为 null", () => {
    expect(deriveVerificationState(null, [])).toBeNull();
    expect(deriveVerificationState(snapshotWith(null), undefined)).toBeNull();
  });

  it("从快照恢复验证结论", () => {
    const state = deriveVerificationState(
      snapshotWith({
        runId: "run_1",
        status: "verified",
        verdict: "pass",
        reasons: [],
        missing: [],
        model: "verifier-model",
      }),
      [],
    );
    expect(state?.verdict).toBe("pass");
    expect(state?.model).toBe("verifier-model");
  });

  it("run.verifying 显示进行中且无结论", () => {
    const state = deriveVerificationState(snapshotWith(null), [
      event("run.verifying", { model: "verifier-model" }),
    ]);
    expect(state?.status).toBe("verifying");
    expect(state?.verdict).toBeNull();
  });

  it("run.verified 覆盖为最终结论并解析字段", () => {
    const state = deriveVerificationState(snapshotWith(null), [
      event("run.verifying"),
      event("run.verified", {
        verdict: "fail",
        reasons: ["报告未写入"],
        missing: ["report.md"],
        model: "verifier-model",
        latencyMs: 42,
        inputTokens: 10,
        outputTokens: 5,
      }),
    ]);
    expect(state?.status).toBe("verified");
    expect(state?.verdict).toBe("fail");
    expect(state?.reasons).toEqual(["报告未写入"]);
    expect(state?.missing).toEqual(["report.md"]);
    expect(state?.latencyMs).toBe(42);
  });

  it("缺字段时容错", () => {
    const state = deriveVerificationState(snapshotWith(null), [
      event("run.verified", {}),
    ]);
    expect(state?.verdict).toBeNull();
    expect(state?.reasons).toEqual([]);
    expect(state?.model).toBe("");
  });
});

describe("验证文案", () => {
  const base: RuntimeV2Verification = {
    runId: "run_1",
    status: "verified",
    verdict: "pass",
    reasons: [],
    missing: [],
    model: "m",
  };

  it("徽标文案覆盖四种状态", () => {
    expect(verificationBadgeText(base)).toBe("独立验证：通过");
    expect(verificationBadgeText({ ...base, verdict: "fail" })).toBe(
      "独立验证：未通过",
    );
    expect(verificationBadgeText({ ...base, verdict: "uncertain" })).toBe(
      "独立验证：不确定",
    );
    expect(
      verificationBadgeText({ ...base, status: "verifying", verdict: null }),
    ).toBe("独立验证中…");
  });

  it("详情合并理由与缺失项", () => {
    expect(
      verificationDetailText({
        ...base,
        verdict: "fail",
        reasons: ["报告未写入", "格式不对"],
        missing: ["report.md"],
      }),
    ).toBe("报告未写入；格式不对；缺失：report.md");
    expect(verificationDetailText(base)).toBe("");
  });

  it("带结论重试指令包含验证者理由与缺失项", () => {
    const message = verificationRetryMessage({
      ...base,
      verdict: "fail",
      reasons: ["报告未写入"],
      missing: ["report.md"],
    });
    expect(message).toContain("报告未写入");
    expect(message).toContain("report.md");
    expect(message).toContain("独立验证未通过");
  });

  it("无理由时重试指令仍有兜底措辞", () => {
    const message = verificationRetryMessage({ ...base, verdict: "fail" });
    expect(message).toContain("独立验证未通过");
    expect(message).toContain("修正");
  });
});
