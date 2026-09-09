import { describe, expect, it } from "vitest";
import { buildTerminalTheme } from "./TerminalPane";

describe("buildTerminalTheme（终端主题跟随产品 token）", () => {
  it("不使用黑底默认值，背景与前景来自产品色", () => {
    const theme = buildTerminalTheme();
    // node 环境下 window 不存在 → 取 fallback（浅色 token）
    expect(theme.background).toBe("#f3f3ee");
    expect(theme.background).not.toBe("#000000");
    expect(theme.foreground).toBe("#30332f");
    expect(theme.cursor).toBe("#275c4b");
    expect(theme.selectionBackground).toBe("#dcebe4");
  });
});
