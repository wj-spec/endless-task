import { describe, expect, it } from "vitest";
import { menuWindow } from "./mentionContext";

/**
 * M-1：`menuWindow` 的 **50 条防御上限**此前没有守护。
 *
 * 它是从用户 `83f8498` 继承下来的语义（`/技能名` 菜单的可滚动上限），C3b 把它抽成
 * 主/侧共用的纯函数时**没有顺带补测**：既有用例最多 12 个候选，够不到上限
 * ⇒ 把 `Math.min(matchCount, options.limit)` 改成 `matchCount` 也 0 红。
 */
describe("menuWindow 的防御上限（M-1）", () => {
  it("候选数超过 limit ⇒ 渲染数截到 limit，多出来的算进 hiddenCount", () => {
    // 60 个候选、上限 50（`ChatComposer` 里 `/` 与 `@` 各自传 50）。
    expect(menuWindow(60, 0, { limit: 50, visibleRows: 8 })).toEqual({
      visibleCount: 50,
      hiddenCount: 10,
      activeIndex: 0,
      // 50 > 8 ⇒ 要补那一行"共 N 条"的统计提示。
      showCount: true,
    });
  });

  it("高亮下标被夹回真正渲染出来的那批（超上限时不指向没渲染的行）", () => {
    expect(menuWindow(60, 59, { limit: 50, visibleRows: 8 }).activeIndex).toBe(49);
    expect(menuWindow(60, -3, { limit: 50, visibleRows: 8 }).activeIndex).toBe(0);
  });

  it("不超过上限时逐字给出全部候选（上限只是防御，不改变正常语义）", () => {
    expect(menuWindow(3, 1, { limit: 50, visibleRows: 8 })).toEqual({
      visibleCount: 3,
      hiddenCount: 0,
      activeIndex: 1,
      showCount: false,
    });
  });
});
