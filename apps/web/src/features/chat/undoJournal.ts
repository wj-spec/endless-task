import type { UndoJournalEntry } from "./apiTypes";

/**
 * A5 撤销/回滚：把撤销日志压成一句人话。
 *
 * 只有可逆操作才有撤销入口；不可逆操作（外部动作、命令副作用）不会出现在这里。
 */

export const isUndoable = (entry: UndoJournalEntry): boolean =>
  entry.undoable && entry.status === "available";

/** 一行说明：做了什么 + 撤销后会发生什么。 */
export const undoNoticeText = (entry: UndoJournalEntry): string =>
  `刚才${entry.description}`;

export const undoImpactText = (entry: UndoJournalEntry): string => {
  if (entry.kind === "file_delete") return `撤销后会恢复 ${entry.target} 的内容`;
  return `撤销后 ${entry.target} 会回到这次改动之前的样子`;
};

export const undoKindLabel = (kind: string): string => {
  if (kind === "file_delete") return "删除文件";
  if (kind === "file_write") return "写入文件";
  return "未知操作";
};

/** 撤销完成后的反馈文案。 */
export const undoDoneText = (
  entry: UndoJournalEntry,
  alreadyUndone: boolean,
): string =>
  alreadyUndone
    ? `「${entry.target}」已经撤销过了，无需重复操作`
    : `已撤销：${entry.description}`;

/** 从列表里挑出最近一条可撤销的操作。 */
export const latestUndoable = (
  entries: UndoJournalEntry[],
): UndoJournalEntry | null =>
  entries.find((entry) => isUndoable(entry)) ?? null;
