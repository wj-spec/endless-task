import { describe, expect, it } from "vitest";
import { clampWidth, LAYOUT_LIMITS } from "./useLayoutWidth";

describe("clampWidth（布局宽度约束）", () => {
  it("夹紧到各自区间并取整", () => {
    expect(clampWidth("rail", 100)).toBe(LAYOUT_LIMITS.rail.min);
    expect(clampWidth("rail", 999)).toBe(LAYOUT_LIMITS.rail.max);
    expect(clampWidth("aux", 301.6)).toBe(302);
    expect(clampWidth("aux", 800)).toBe(LAYOUT_LIMITS.aux.max);
  });

  it("默认值落在区间内", () => {
    for (const pane of ["rail", "aux"] as const) {
      const { min, max, fallback } = LAYOUT_LIMITS[pane];
      expect(fallback).toBeGreaterThanOrEqual(min);
      expect(fallback).toBeLessThanOrEqual(max);
    }
  });
});
