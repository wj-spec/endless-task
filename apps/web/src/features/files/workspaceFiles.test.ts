import { describe, expect, it } from "vitest";
import type { WorkspaceTreeEntry } from "../chat/apiTypes";
import {
  childPath,
  copyNameFor,
  diffLines,
  diffSummary,
  extensionOf,
  formatFileSize,
  isMarkdownPath,
  isNoisyEntry,
  languageForPath,
  parentPath,
  pathSegments,
} from "./workspaceFiles";

const entry = (over: Partial<WorkspaceTreeEntry> = {}): WorkspaceTreeEntry => ({
  name: "main.py",
  relativePath: "src/main.py",
  kind: "file",
  size: 128,
  isHidden: false,
  ...over,
});

describe("workspaceFiles（文件面板纯函数）", () => {
  it("解析扩展名与 Markdown 判定", () => {
    expect(extensionOf("src/main.py")).toBe("py");
    expect(extensionOf("README.MD")).toBe("md");
    expect(extensionOf("Makefile")).toBe("");
    expect(extensionOf("archive.tar.gz")).toBe("gz");
    expect(isMarkdownPath("docs/plan.md")).toBe(true);
    expect(isMarkdownPath("docs/plan.markdown")).toBe(true);
    expect(isMarkdownPath("docs/plan.txt")).toBe(false);
  });

  it("扩展名映射到高亮语言", () => {
    expect(languageForPath("a.tsx")).toBe("typescript");
    expect(languageForPath("a.yml")).toBe("yaml");
    expect(languageForPath("a.unknown")).toBe("");
  });

  it("格式化文件大小，目录不显示大小", () => {
    expect(formatFileSize(entry({ size: 512 }))).toBe("512 B");
    expect(formatFileSize(entry({ size: 2048 }))).toBe("2.0 KB");
    expect(formatFileSize(entry({ size: 3 * 1024 * 1024 }))).toBe("3.0 MB");
    expect(formatFileSize(entry({ kind: "directory" }))).toBe("");
  });

  it("识别降噪目录", () => {
    expect(isNoisyEntry(entry({ kind: "directory", name: "node_modules" }))).toBe(
      true,
    );
    expect(isNoisyEntry(entry({ kind: "directory", name: "src" }))).toBe(false);
    expect(isNoisyEntry(entry({ name: "node_modules" }))).toBe(false);
  });

  it("拼装与拆解相对路径", () => {
    expect(parentPath("src/features/chat.ts")).toBe("src/features");
    expect(parentPath("README.md")).toBe("");
    expect(childPath("src", "main.py")).toBe("src/main.py");
    expect(childPath("", "main.py")).toBe("main.py");
    expect(pathSegments("src/features/chat.ts")).toEqual([
      { label: "src", path: "src" },
      { label: "features", path: "src/features" },
      { label: "chat.ts", path: "src/features/chat.ts" },
    ]);
    expect(pathSegments("")).toEqual([]);
  });

  it("另存副本文件名带时间戳且保留目录与扩展名", () => {
    const now = new Date(2026, 8, 9, 12, 3);
    expect(copyNameFor("note.md", now)).toBe("note.edited-20260909-1203.md");
    expect(copyNameFor("src/note.md", now)).toBe("src/note.edited-20260909-1203.md");
    expect(copyNameFor("Makefile", now)).toBe("Makefile.edited-20260909-1203");
  });

  it("行级 diff：公共前后缀保持 same，中间给出增删", () => {
    const lines = diffLines("a\nb\nc\n", "a\nB\nc\n");
    expect(lines.map((line) => [line.type, line.text])).toEqual([
      ["same", "a"],
      ["remove", "b"],
      ["add", "B"],
      ["same", "c"],
      ["same", ""],
    ]);
    expect(diffSummary(lines)).toEqual({ added: 1, removed: 1 });
  });

  it("行级 diff：整段新增与整段删除", () => {
    expect(diffSummary(diffLines("", "a\nb"))).toEqual({ added: 2, removed: 1 });
    expect(diffSummary(diffLines("a\nb", ""))).toEqual({ added: 1, removed: 2 });
    expect(diffSummary(diffLines("same", "same"))).toEqual({ added: 0, removed: 0 });
  });
});
