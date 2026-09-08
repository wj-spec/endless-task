import { describe, expect, it } from "vitest";
import type { MemoryRecord } from "../chat/apiTypes";
import {
  forgetRiskLabel,
  formatDaysSince,
  importanceLabel,
  isImportant,
  retentionHint,
} from "./memoryRetention";

const memory: MemoryRecord = {
  id: "mem_1",
  kind: "fact",
  content: "用户偏好周五发布。",
  status: "active",
  sourceConversationId: "conv_1",
  sourceConversationTitle: null,
  sourceTurnId: "turn_1",
  writeOrigin: "confirmed_proposal",
  createdAt: "2026-01-01T00:00:00+00:00",
  updatedAt: "2026-01-01T00:00:00+00:00",
  sourceProposalId: null,
  expiredReason: null,
  supersededBy: null,
  importance: 0.5,
  accessCount: 0,
  lastAccessedAt: null,
  pinned: false,
};

describe("memoryRetention（B3 遗忘可见性）", () => {
  it("重要性就近映射到档位", () => {
    expect(importanceLabel(0.1)).toBe("不重要");
    expect(importanceLabel(0.5)).toBe("普通");
    expect(importanceLabel(0.75)).toBe("重要");
    expect(importanceLabel(1)).toBe("关键");
    expect(importanceLabel(Number.NaN)).toBe("普通");
  });

  it("0.7 及以上算重要", () => {
    expect(isImportant(0.7)).toBe(true);
    expect(isImportant(0.69)).toBe(false);
  });

  it("保留提示包含钉住/访问/最近使用", () => {
    expect(retentionHint(memory)).toBe("还没被用到过");
    expect(
      retentionHint(
        {
          ...memory,
          pinned: true,
          accessCount: 3,
          lastAccessedAt: "2026-01-30T00:00:00+00:00",
        },
        Date.parse("2026-01-31T00:00:00+00:00"),
      ),
    ).toBe("已钉住 · 被用到 3 次 · 最近 昨天用过");
  });

  it("按天数给可读时间", () => {
    const now = Date.parse("2026-02-10T00:00:00+00:00");
    expect(formatDaysSince("2026-02-10T00:00:00+00:00", now)).toBe("今天用过");
    expect(formatDaysSince("2026-02-09T00:00:00+00:00", now)).toBe("昨天用过");
    expect(formatDaysSince("2026-02-05T00:00:00+00:00", now)).toBe("5 天前用过");
    expect(formatDaysSince("2025-12-01T00:00:00+00:00", now)).toBe(
      "2 个月前用过",
    );
    expect(formatDaysSince("not-a-date", now)).toBe("时间未知");
  });

  it("遗忘概率映射到风险文案", () => {
    expect(forgetRiskLabel(0.9)).toBe("很可能被忘");
    expect(forgetRiskLabel(0.6)).toBe("可能被忘");
    expect(forgetRiskLabel(0.3)).toBe("暂时安全");
    expect(forgetRiskLabel(0.05)).toBe("很难被忘");
  });
});
