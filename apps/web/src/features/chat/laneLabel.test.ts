import { describe, expect, it } from "vitest";
import type { RuntimeV2Lane } from "./apiTypes";
import { friendlyLaneName } from "./laneLabel";

const lane = (over: Partial<RuntimeV2Lane>): RuntimeV2Lane => ({
  id: "lane-1",
  conversationId: "c-1",
  kind: "persistent_branch",
  status: "active",
  archived: false,
  archivedAt: null,
  displayName: null,
  summary: null,
  title: null,
  baseEntryExcerpt: null,
  baseEntryId: null,
  leafEntryId: null,
  createdFromEntryId: null,
  createdAt: "2026-09-06T00:00:00Z",
  sourceLaneId: null,
  isMain: false,
  ...over,
});

describe("friendlyLaneName", () => {
  it("无 lane 时返回回退名", () => {
    expect(friendlyLaneName(null)).toBe("未命名分支");
    expect(friendlyLaneName(undefined, "另一分支")).toBe("另一分支");
  });

  it("优先使用用户命名的 displayName", () => {
    expect(
      friendlyLaneName(lane({ displayName: "对照实验A" })),
    ).toBe("对照实验A");
  });

  it("去掉 Markdown 语法符号（强调/链接/表格竖线）", () => {
    const cleaned = friendlyLaneName(
      lane({ baseEntryExcerpt: "**目录** | docs/ 与 *文件* chat1.md" }),
    );
    expect(cleaned).toBe("目录 docs/ 与 文件 chat1.md");
  });

  it("回退到 title/summary 并截断到指定长度", () => {
    const long = "这是一个非常长的分支摘要内容用来测试截断行为是否正确省略号";
    expect(friendlyLaneName(lane({ summary: long }), "未命名分支", 10)).toBe(
      "这是一个非常长的分支…",
    );
  });

  it("空白内容时返回回退名", () => {
    expect(friendlyLaneName(lane({ summary: "  " }))).toBe("未命名分支");
  });
});
