import { describe, expect, it } from "vitest";
import type { MemoryReflectionRecord } from "../chat/apiTypes";
import {
  reflectionSourceText,
  reflectionStatusLabel,
  reflectionTriggerLabel,
} from "./memoryReflection";

const record: MemoryReflectionRecord = {
  id: "mref_1",
  conversationId: "conv_1",
  runId: "run_abcdef123456",
  trigger: "tool_failure",
  status: "pending",
  insight: "工具 read_workspace_file 曾因 path_not_found 连续失败 2 次：下次先确认路径。",
  proposalId: "mprop_1",
  insightMemoryId: null,
  createdAt: "2026-01-01T00:00:00+00:00",
  resolvedAt: null,
  sources: [
    {
      refs: [
        {
          toolName: "read_workspace_file",
          errorCode: "path_not_found",
          runId: "run_abcdef123456",
        },
      ],
    },
  ],
};

describe("memoryReflection（B4 反思文案）", () => {
  it("触发原因映射到可读文案", () => {
    expect(reflectionTriggerLabel("tool_failure")).toBe("工具反复失败");
    expect(reflectionTriggerLabel("run_failure")).toBe("运行失败");
    expect(reflectionTriggerLabel("escalation:no_progress")).toBe("连续无进展");
    expect(reflectionTriggerLabel("escalation:verification_failed")).toBe(
      "独立验证未通过",
    );
    expect(reflectionTriggerLabel("mystery")).toBe("反思");
  });

  it("状态映射到可读文案", () => {
    expect(reflectionStatusLabel("pending")).toBe("待确认");
    expect(reflectionStatusLabel("accepted")).toBe("已记住");
    expect(reflectionStatusLabel("rejected")).toBe("已忽略");
  });

  it("来源摘要包含运行、工具与错误码", () => {
    const text = reflectionSourceText(record);
    expect(text).toContain("运行 …123456");
    expect(text).toContain("read_workspace_file");
    expect(text).toContain("path_not_found");
  });

  it("来源缺失时给兜底文案", () => {
    expect(reflectionSourceText({ ...record, sources: [], runId: null })).toBe(
      "来源：本次运行",
    );
  });
});
