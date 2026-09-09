import { describe, expect, it } from "vitest";
import { computeMenuPosition } from "./RowMenu";

const viewport = { width: 1200, height: 800 };
const trigger = { top: 100, bottom: 130, left: 900, right: 930 };
const menu = { width: 160, height: 120 };

describe("computeMenuPosition（浮层定位）", () => {
  it("down：右对齐触发点、位于下方", () => {
    expect(
      computeMenuPosition({ triggerRect: trigger, menu, viewport, placement: "down" }),
    ).toEqual({ top: 136, left: 770 });
  });

  it("up：左对齐触发点、位于上方（顶部越界夹到边距）", () => {
    expect(
      computeMenuPosition({ triggerRect: trigger, menu, viewport, placement: "up" }),
    ).toEqual({ top: 8, left: 900 });
    // 空间充足时正常在触发点上方
    expect(
      computeMenuPosition({
        triggerRect: { ...trigger, top: 400, bottom: 430 },
        menu,
        viewport,
        placement: "up",
      }),
    ).toEqual({ top: 274, left: 900 });
  });

  it("右侧越界时夹回视口内", () => {
    const right = { top: 100, bottom: 130, left: 1180, right: 1200 };
    const { left } = computeMenuPosition({
      triggerRect: right,
      menu,
      viewport,
      placement: "down",
    });
    expect(left).toBe(viewport.width - menu.width - 8);
  });

  it("左侧越界时夹到边距", () => {
    const leftEdge = { top: 100, bottom: 130, left: 0, right: 10 };
    const { left } = computeMenuPosition({
      triggerRect: leftEdge,
      menu,
      viewport,
      placement: "down",
    });
    expect(left).toBe(8);
  });

  it("底部空间不足时向上收拢", () => {
    const low = { top: 760, bottom: 790, left: 100, right: 130 };
    const { top } = computeMenuPosition({
      triggerRect: low,
      menu,
      viewport,
      placement: "down",
    });
    expect(top).toBe(viewport.height - menu.height - 8);
  });
});
