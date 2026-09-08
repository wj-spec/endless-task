import { describe, expect, it } from "vitest";
import type { AuditTrailEntry } from "./apiTypes";
import {
  auditKindLabel,
  auditOptionLabel,
  auditSummaryLine,
  hasWhy,
  orderAuditEntries,
  severityLabel,
  severityRank,
} from "./auditTrail";

const entry = (overrides: Partial<AuditTrailEntry> = {}): AuditTrailEntry => ({
  id: "e1",
  kind: "tool",
  title: "write_workspace_file 执行完成",
  summary: "",
  rationale: "这一步会改动数据（local_write）",
  counterfactual: "如果不批准，文件不会被改动",
  uncertainty: "",
  severity: "warning",
  occurredAt: "2026-01-01T00:00:00+00:00",
  toolName: "write_workspace_file",
  effect: "local_write",
  risk: "",
  decision: "",
  options: [],
  refs: {},
  ...overrides,
});

describe("auditTrail（A8 轨迹展示）", () => {
  it("类型与严重度有可读标签", () => {
    expect(auditKindLabel("approval")).toBe("审批");
    expect(auditKindLabel("mystery")).toBe("记录");
    expect(severityLabel("critical")).toBe("需要留意");
    expect(severityLabel("warning")).toBe("有影响");
    expect(severityLabel("info")).toBe("常规");
    expect(severityRank("critical")).toBeLessThan(severityRank("info"));
  });

  it("有理由/反事实/不确定性才显示为什么", () => {
    expect(hasWhy(entry())).toBe(true);
    expect(
      hasWhy(entry({ rationale: "", counterfactual: "", uncertainty: "" })),
    ).toBe(false);
  });

  it("默认把需要留意的排前面，其次按时间倒序", () => {
    const ordered = orderAuditEntries([
      entry({ id: "info", severity: "info", occurredAt: "2026-01-01T00:00:03+00:00" }),
      entry({ id: "warn-old", severity: "warning", occurredAt: "2026-01-01T00:00:01+00:00" }),
      entry({ id: "crit", severity: "critical", occurredAt: "2026-01-01T00:00:02+00:00" }),
      entry({ id: "warn-new", severity: "warning", occurredAt: "2026-01-01T00:00:05+00:00" }),
    ]);
    expect(ordered.map((item) => item.id)).toEqual([
      "crit",
      "warn-new",
      "warn-old",
      "info",
    ]);
  });

  it("摘要行统计总数与需留意数量", () => {
    expect(
      auditSummaryLine({ info: 2, warning: 1, critical: 1 }),
    ).toBe("共 4 条 · 1 条需要留意 · 1 条有影响");
    expect(auditSummaryLine({ info: 3 })).toBe("共 3 条");
  });

  it("升级选项映射成中文", () => {
    expect(auditOptionLabel("continue")).toBe("继续");
    expect(auditOptionLabel("change_approach")).toBe("换一条路径");
    expect(auditOptionLabel("take_over")).toBe("人工接管");
    expect(auditOptionLabel("other")).toBe("other");
  });
});
