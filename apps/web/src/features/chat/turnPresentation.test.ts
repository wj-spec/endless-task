/**
 * `turnPresentation`：从 `ChatWorkSurface.tsx` 抽出的轮次展示层纯函数。
 *
 * 这些函数此前没有单测，但承载着两条产品规则：
 * ① 后端没下发审批风险等级时，按**工具名**兜底推导（删除类一律高危，其余中危）；
 * ② 计划条展示的是该 run 的**最后一条** plan 条目，且载荷形状不对时返回 null。
 */
import { describe, expect, it } from "vitest";
import type { ResponseVariantSnapshot, RuntimeV2Snapshot } from "./apiTypes";
import {
  APPROVAL_RISK_LABELS,
  approvalRisk,
  findActiveVariant,
  latestPlanForRun,
  turnStatusPresentation,
} from "./turnPresentation";

describe("turnStatusPresentation（轮次状态徽标）", () => {
  it("等待审批优先于运行状态", () => {
    expect(turnStatusPresentation("running", true)).toEqual({
      label: "等待确认",
      tone: "warning",
      pulse: false,
    });
  });

  it("created / running 是进行态（带脉冲）", () => {
    expect(turnStatusPresentation("created", false)).toMatchObject({
      label: "准备回答",
      pulse: true,
    });
    expect(turnStatusPresentation("running", false)).toMatchObject({
      label: "正在回答",
      pulse: true,
    });
  });

  it("failed / cancelled 是终态（不脉冲）", () => {
    expect(turnStatusPresentation("failed", false)).toMatchObject({
      label: "回答失败",
      tone: "danger",
      pulse: false,
    });
    expect(turnStatusPresentation("cancelled", false)).toMatchObject({
      label: "已停止",
      tone: "neutral",
      pulse: false,
    });
  });

  it("completed 不显示徽标", () => {
    expect(turnStatusPresentation("completed", false)).toBeNull();
  });
});

describe("approvalRisk（风险兜底推导）", () => {
  it("后端下发了合法等级时直接用", () => {
    expect(approvalRisk({ risk: "low" })).toBe("low");
    expect(approvalRisk({ risk: "high" })).toBe("high");
  });

  it("没下发等级时按工具名兜底：删除类高危，其余中危", () => {
    for (const name of [
      "delete_workspace_file",
      "remove_file",
      "DROP_TABLE",
      "truncate_log",
      "wipe_cache",
      "unlink_path",
    ]) {
      expect(approvalRisk({ toolName: name })).toBe("high");
    }
    expect(approvalRisk({ toolName: "write_workspace_file" })).toBe("medium");
  });

  it("非法等级与缺失元数据都退到中危", () => {
    expect(approvalRisk({ risk: "unknown" })).toBe("medium");
    expect(approvalRisk({})).toBe("medium");
    expect(approvalRisk(undefined)).toBe("medium");
  });

  it("风险标签齐全", () => {
    expect(Object.keys(APPROVAL_RISK_LABELS).sort()).toEqual([
      "high",
      "low",
      "medium",
    ]);
  });
});

describe("latestPlanForRun（取该 run 的最后一版计划）", () => {
  const snapshot = (
    entries: Record<string, unknown>[],
  ): RuntimeV2Snapshot =>
    ({ entries } as unknown as RuntimeV2Snapshot);

  it("没有快照 / 没有 runId / 没有计划条目时返回 null", () => {
    expect(latestPlanForRun(null, "run_1")).toBeNull();
    expect(latestPlanForRun(snapshot([]), "")).toBeNull();
    expect(
      latestPlanForRun(
        snapshot([
          { type: "message", sourceRunId: "run_1", data: {} },
        ]),
        "run_1",
      ),
    ).toBeNull();
  });

  it("只取该 run 的计划，且取最后一条", () => {
    const plan = (title: string, sourceRunId: string) => ({
      type: "plan",
      sourceRunId,
      data: { reference: { title, steps: [{ text: "步骤" }], currentStepIndex: 0 } },
    });
    const result = latestPlanForRun(
      snapshot([
        plan("旧计划", "run_1"),
        plan("别的 run", "run_2"),
        plan("新计划", "run_1"),
      ]),
      "run_1",
    );
    expect(result).toMatchObject({ title: "新计划", currentStepIndex: 0 });
  });

  it("reference 不是对象时返回 null（不抛错）", () => {
    expect(
      latestPlanForRun(
        snapshot([
          { type: "plan", sourceRunId: "run_1", data: { reference: "文本" } },
        ]),
        "run_1",
      ),
    ).toBeNull();
  });
});

describe("findActiveVariant（当前回答变体）", () => {
  const variants = [
    { variant: { id: "v1" } },
    { variant: { id: "v2" } },
  ] as unknown as ResponseVariantSnapshot[];

  it("命中激活 id 时返回该变体", () => {
    expect(findActiveVariant(variants, "v1")).toBe(variants[0]);
  });

  it("激活 id 为空或不存在时退到最后一个（刚生成完的变体）", () => {
    expect(findActiveVariant(variants, null)).toBe(variants[1]);
    expect(findActiveVariant(variants, "missing")).toBe(variants[1]);
  });

  it("没有变体时返回 undefined（调用方据此跳过该轮）", () => {
    expect(findActiveVariant([], null)).toBeUndefined();
  });
});
