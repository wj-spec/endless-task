import { describe, expect, it } from "vitest";
import { collapsePreview } from "./CollapsibleMessage";

const fenceCount = (text: string) => (text.match(/^\s*```/gm) ?? []).length;

describe("collapsePreview（P0-1 按块折叠）", () => {
  it("短内容原样返回", () => {
    const content = "短文本";
    expect(collapsePreview(content)).toBe(content);
  });

  it("在空行边界截断，不切断段落 token", () => {
    const paragraph = (n: number) => `第${n}段内容 ${"词 ".repeat(60)}结尾${n}`;
    const content = Array.from({ length: 10 }, (_, index) => paragraph(index)).join(
      "\n\n",
    );
    const preview = collapsePreview(content, 600);
    expect(preview.endsWith("…")).toBe(true);
    // 预览以整段收尾：截断点之后的段落不应出现在预览中
    expect(preview).not.toContain("结尾9");
    expect(preview.length).toBeLessThan(content.length);
  });

  it("围栏在预览内保持成对", () => {
    const longCode = Array.from({ length: 120 }, (_, index) => `line_${index}()`).join(
      "\n",
    );
    const content = `前言\n\n\`\`\`python\n${longCode}\n\`\`\`\n\n结论部分`;
    const preview = collapsePreview(content, 600);
    expect(fenceCount(preview) % 2).toBe(0);
    expect(preview.endsWith("…")).toBe(true);
  });

  it("巨型单行按空白切，不切在单词中间", () => {
    const content = Array.from({ length: 400 }, (_, index) => `word${index}`).join(" ");
    const preview = collapsePreview(content, 600);
    const body = preview.slice(0, -1); // 去掉 …
    const trimmed = body.replace(/\s+$/, "");
    // 截断处位于空白之后：trimmed 以完整词结尾
    expect(/^\S+$/.test(trimmed.split(" ").pop() ?? "")).toBe(true);
    expect(preview.length).toBeLessThan(content.length);
  });

  it("截断点不会把粗体 token 切开（有边界时按整段返回）", () => {
    const content = [
      "**完整粗体第一段** ".repeat(20).trim(),
      "",
      "第二段开头的 **粗体** 内容结束",
      "",
      "结尾段",
    ].join("\n");
    const preview = collapsePreview(content, 200);
    expect(preview.endsWith("…")).toBe(true);
    // 若预览包含粗体 token，则必须成对
    const boldPairs = (preview.match(/\*\*/g) ?? []).length;
    expect(boldPairs % 2).toBe(0);
  });
});
