import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { FileConflictDialog, FileDiffView } from "./FileEditor";

describe("FileDiffView（保存后变更回显）", () => {
  it("标注新增与删除行并给出统计", () => {
    const html = renderToStaticMarkup(
      <FileDiffView
        after={"标题\n新行\n"}
        before={"标题\n旧行\n"}
        onClose={() => undefined}
      />,
    );
    expect(html).toContain("本次保存：+1 / −1");
    expect(html).toContain("is-add");
    expect(html).toContain("is-remove");
    expect(html).toContain("新行");
    expect(html).toContain("旧行");
  });
});

describe("FileConflictDialog（版本冲突三选一）", () => {
  it("给出重新加载 / 覆盖保存 / 另存为副本三个出口", () => {
    const html = renderToStaticMarkup(
      <FileConflictDialog
        busy={false}
        details={{ currentVersion: "v2", currentContent: "当前内容" }}
        message="文件已被其它改动更新。"
        onCancel={() => undefined}
        onOverwrite={() => undefined}
        onReload={() => undefined}
        onSaveCopy={() => undefined}
      />,
    );
    expect(html).toContain('role="alertdialog"');
    expect(html).toContain("文件已被其它改动更新。");
    expect(html).toContain("重新加载（丢弃我的编辑）");
    expect(html).toContain("覆盖保存");
    expect(html).toContain("另存为副本");
    expect(html).toContain("取消");
  });

  it("原文件已被删除时明确提示", () => {
    const html = renderToStaticMarkup(
      <FileConflictDialog
        busy
        details={{ beforeExists: false }}
        message="冲突"
        onCancel={() => undefined}
        onOverwrite={() => undefined}
        onReload={() => undefined}
        onSaveCopy={() => undefined}
      />,
    );
    expect(html).toContain("原文件已被删除");
    expect(html).toContain("disabled");
  });
});
