import { describe, expect, it } from "vitest";
import { skillCommandsOf } from "./skillCommand";

describe("skillCommandsOf（用户显式指定技能）", () => {
  it("行首与空白后的 /技能名 都识别", () => {
    expect(skillCommandsOf("/archify 画个图")).toEqual(["archify"]);
    expect(skillCommandsOf("先 /archify 再 /review-notes")).toEqual([
      "archify",
      "review-notes",
    ]);
  });

  it("路径与普通文本不算技能", () => {
    expect(skillCommandsOf("看看 /Users/example/x")).toEqual([]);
    expect(skillCommandsOf("3/4 比例")).toEqual([]);
  });

  it("去重且最多三个", () => {
    expect(skillCommandsOf("/a /a /a")).toEqual(["a"]);
    expect(skillCommandsOf("/a /b /c /d")).toEqual(["a", "b", "c"]);
  });
});
