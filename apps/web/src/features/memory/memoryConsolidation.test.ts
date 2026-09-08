import { describe, expect, it } from "vitest";
import type { MemoryConsolidationRecord } from "../chat/apiTypes";
import {
  consolidationSourcePreview,
  consolidationStatusLabel,
  consolidationSummary,
  mergedIntoText,
} from "./memoryConsolidation";

const record: MemoryConsolidationRecord = {
  id: "mcon_1",
  proposalId: "mprop_1",
  kind: "fact",
  status: "accepted",
  signature: "sig",
  insightMemoryId: "mem_insight",
  createdAt: "2026-01-01T00:00:00+00:00",
  resolvedAt: "2026-01-02T00:00:00+00:00",
  sources: [
    { id: "mem_1", content: "用户偏好周五发布版本", status: "expired" },
    { id: "mem_2", content: "用户偏好周五发布新版本", status: "expired" },
  ],
};

describe("memoryConsolidation（B2 巩固文案）", () => {
  it("状态映射到可读文案", () => {
    expect(consolidationStatusLabel("pending")).toBe("待确认");
    expect(consolidationStatusLabel("accepted")).toBe("已并入");
    expect(consolidationStatusLabel("rejected")).toBe("已放弃");
  });

  it("摘要包含来源数量与状态", () => {
    expect(consolidationSummary(record)).toBe("合并 2 条同类记忆 · 已并入");
  });

  it("已并入说明区分有无洞察记忆", () => {
    expect(mergedIntoText(record)).toContain("更高层的记忆");
    expect(
      mergedIntoText({ ...record, insightMemoryId: null }),
    ).toBe("已并入一条更高层的记忆");
  });

  it("来源预览限制条数", () => {
    expect(consolidationSourcePreview(record, 1)).toEqual([
      "用户偏好周五发布版本",
    ]);
    expect(consolidationSourcePreview(record)).toHaveLength(2);
  });
});
