import { afterEach, describe, expect, it, vi } from "vitest";
import { formatRelativeTime } from "./time";

const NOW = "2026-09-06T12:00:00Z";

describe("formatRelativeTime", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  const at = (iso: string) => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(NOW));
    return formatRelativeTime(iso);
  };

  it("无效时间返回空串", () => {
    expect(at("not-a-date")).toBe("");
  });

  it("分/时/天/昨日与更早日期分档", () => {
    expect(at("2026-09-06T11:59:30Z")).toBe("刚刚");
    expect(at("2026-09-06T11:30:00Z")).toBe("30 分钟前");
    expect(at("2026-09-06T09:00:00Z")).toBe("3 小时前");
    expect(at("2026-09-05T08:00:00Z")).toBe("昨天");
    expect(at("2026-09-03T08:00:00Z")).toBe("3 天前");
    expect(at("2026-08-20T08:00:00Z")).toBe("8 月 20 日");
  });
});
