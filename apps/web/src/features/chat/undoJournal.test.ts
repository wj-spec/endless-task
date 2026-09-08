import { describe, expect, it } from "vitest";
import type { UndoJournalEntry } from "./apiTypes";
import {
  isUndoable,
  latestUndoable,
  undoDoneText,
  undoImpactText,
  undoKindLabel,
  undoNoticeText,
} from "./undoJournal";

const entry: UndoJournalEntry = {
  id: "undo_1",
  conversationId: "conv_1",
  workspaceId: "ws_1",
  runId: "run_1",
  kind: "file_write",
  target: "docs/plan.md",
  description: "覆盖了文件 docs/plan.md",
  status: "available",
  undoable: true,
  createdAt: "2026-01-01T00:00:00+00:00",
  undoneAt: null,
};

describe("undoJournal（A5 撤销文案）", () => {
  it("可用性判定看 status 与 undoable", () => {
    expect(isUndoable(entry)).toBe(true);
    expect(isUndoable({ ...entry, status: "undone" })).toBe(false);
    expect(isUndoable({ ...entry, undoable: false })).toBe(false);
  });

  it("提示文案包含描述与影响", () => {
    expect(undoNoticeText(entry)).toBe("刚才覆盖了文件 docs/plan.md");
    expect(undoImpactText(entry)).toBe(
      "撤销后 docs/plan.md 会回到这次改动之前的样子",
    );
    expect(
      undoImpactText({ ...entry, kind: "file_delete", target: "a.txt" }),
    ).toBe("撤销后会恢复 a.txt 的内容");
  });

  it("操作类型有可读标签", () => {
    expect(undoKindLabel("file_write")).toBe("写入文件");
    expect(undoKindLabel("file_delete")).toBe("删除文件");
    expect(undoKindLabel("shell")).toBe("未知操作");
  });

  it("撤销完成文案区分首次与重复", () => {
    expect(undoDoneText(entry, false)).toContain("已撤销");
    expect(undoDoneText(entry, true)).toContain("已经撤销过");
  });

  it("挑出最近一条可撤销操作", () => {
    const undone = { ...entry, id: "undo_2", status: "undone", undoable: false };
    expect(latestUndoable([undone, entry])?.id).toBe("undo_1");
    expect(latestUndoable([undone])).toBeNull();
  });
});
