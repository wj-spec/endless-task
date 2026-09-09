import type { WorkspaceTreeEntry } from "../chat/apiTypes";

/** Markdown 扩展名（预览走 MessageContent 渲染）。 */
const MARKDOWN_EXTENSIONS = new Set(["md", "markdown", "mdx"]);

/** 扩展名 → highlight.js 语言名（未登记的走纯文本）。 */
const LANGUAGE_BY_EXTENSION: Record<string, string> = {
  bash: "bash",
  c: "c",
  cc: "cpp",
  cpp: "cpp",
  css: "css",
  diff: "diff",
  go: "go",
  h: "c",
  hpp: "cpp",
  htm: "xml",
  html: "xml",
  java: "java",
  js: "javascript",
  json: "json",
  jsx: "javascript",
  md: "markdown",
  mjs: "javascript",
  cjs: "javascript",
  py: "python",
  rb: "ruby",
  rs: "rust",
  sh: "bash",
  sql: "sql",
  svg: "xml",
  ts: "typescript",
  tsx: "typescript",
  xml: "xml",
  yaml: "yaml",
  yml: "yaml",
  zsh: "bash",
};

/** 目录树里默认降噪的目录（仍可见，只是弱化显示）。 */
const NOISY_DIRECTORIES = new Set([
  ".git",
  ".next",
  ".turbo",
  ".venv",
  "__pycache__",
  "build",
  "dist",
  "node_modules",
  "target",
  "vendor",
]);

/** 文件扩展名（小写、不含点）；没有扩展名时返回空串。 */
export function extensionOf(path: string): string {
  const name = path.split("/").pop() ?? path;
  const index = name.lastIndexOf(".");
  if (index <= 0 || index === name.length - 1) return "";
  return name.slice(index + 1).toLowerCase();
}

export function isMarkdownPath(path: string): boolean {
  return MARKDOWN_EXTENSIONS.has(extensionOf(path));
}

/** 代码预览用的语言名；未知扩展名返回空串（CodeBlock 回退纯文本）。 */
export function languageForPath(path: string): string {
  return LANGUAGE_BY_EXTENSION[extensionOf(path)] ?? "";
}

export function isNoisyEntry(entry: WorkspaceTreeEntry): boolean {
  return entry.kind === "directory" && NOISY_DIRECTORIES.has(entry.name);
}

/** 人类可读的文件大小（B/KB/MB），目录固定为空串。 */
export function formatFileSize(entry: WorkspaceTreeEntry): string {
  if (entry.kind === "directory") return "";
  const bytes = entry.size;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** 父目录的 relativePath（根层条目返回空串）。 */
export function parentPath(relativePath: string): string {
  const index = relativePath.lastIndexOf("/");
  return index < 0 ? "" : relativePath.slice(0, index);
}

/** 拼出子条目的 relativePath。 */
export function childPath(parent: string, name: string): string {
  return parent ? `${parent}/${name}` : name;
}

/** 面包屑片段：根显示工作区名，其余显示路径段。 */
export function pathSegments(relativePath: string): { label: string; path: string }[] {
  if (!relativePath) return [];
  const parts = relativePath.split("/").filter(Boolean);
  return parts.map((label, index) => ({
    label,
    path: parts.slice(0, index + 1).join("/"),
  }));
}

/** 另存副本的目标路径：`note.md` → `note.edited-20260909-1203.md`。 */
export function copyNameFor(relativePath: string, now = new Date()): string {
  const slash = relativePath.lastIndexOf("/");
  const dir = slash < 0 ? "" : relativePath.slice(0, slash + 1);
  const name = slash < 0 ? relativePath : relativePath.slice(slash + 1);
  const dot = name.lastIndexOf(".");
  const stem = dot > 0 ? name.slice(0, dot) : name;
  const ext = dot > 0 ? name.slice(dot) : "";
  const stamp = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
  ].join("")
    + "-"
    + [
      String(now.getHours()).padStart(2, "0"),
      String(now.getMinutes()).padStart(2, "0"),
    ].join("");
  return `${dir}${stem}.edited-${stamp}${ext}`;
}

export type DiffLine = {
  type: "same" | "add" | "remove";
  text: string;
};

/** 超过这个规模就放弃精细 LCS，直接整体替换（避免大文件卡住主线程）。 */
const DIFF_LCS_LIMIT = 400;

/**
 * 行级 diff：先裁掉公共前后缀，再对中间部分做 LCS；中间过大时退化为
 * "整段删除 + 整段新增"。用于保存后的变更回显，不追求最小编辑距离。
 */
export function diffLines(before: string, after: string): DiffLine[] {
  const left = before.split("\n");
  const right = after.split("\n");
  let start = 0;
  while (start < left.length && start < right.length && left[start] === right[start]) {
    start += 1;
  }
  let endLeft = left.length;
  let endRight = right.length;
  while (
    endLeft > start &&
    endRight > start &&
    left[endLeft - 1] === right[endRight - 1]
  ) {
    endLeft -= 1;
    endRight -= 1;
  }
  const head: DiffLine[] = left
    .slice(0, start)
    .map((text) => ({ type: "same", text }));
  const tail: DiffLine[] = left
    .slice(endLeft)
    .map((text) => ({ type: "same", text }));
  const midLeft = left.slice(start, endLeft);
  const midRight = right.slice(start, endRight);

  if (midLeft.length === 0 && midRight.length === 0) return head.concat(tail);
  if (midLeft.length + midRight.length > DIFF_LCS_LIMIT) {
    return [
      ...head,
      ...midLeft.map((text): DiffLine => ({ type: "remove", text })),
      ...midRight.map((text): DiffLine => ({ type: "add", text })),
      ...tail,
    ];
  }

  // LCS DP（滚动一维数组，O(n*m) 时间 / O(min) 空间）。
  const rows = midLeft.length;
  const cols = midRight.length;
  const table: number[][] = Array.from({ length: rows + 1 }, () =>
    new Array<number>(cols + 1).fill(0),
  );
  for (let i = rows - 1; i >= 0; i -= 1) {
    for (let j = cols - 1; j >= 0; j -= 1) {
      table[i][j] =
        midLeft[i] === midRight[j]
          ? table[i + 1][j + 1] + 1
          : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const middle: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < rows && j < cols) {
    if (midLeft[i] === midRight[j]) {
      middle.push({ type: "same", text: midLeft[i] });
      i += 1;
      j += 1;
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      middle.push({ type: "remove", text: midLeft[i] });
      i += 1;
    } else {
      middle.push({ type: "add", text: midRight[j] });
      j += 1;
    }
  }
  while (i < rows) {
    middle.push({ type: "remove", text: midLeft[i] });
    i += 1;
  }
  while (j < cols) {
    middle.push({ type: "add", text: midRight[j] });
    j += 1;
  }
  return [...head, ...middle, ...tail];
}

/** 保存后用于提示的变更统计（只统计新增/删除行数）。 */
export function diffSummary(lines: DiffLine[]): { added: number; removed: number } {
  let added = 0;
  let removed = 0;
  for (const line of lines) {
    if (line.type === "add") added += 1;
    if (line.type === "remove") removed += 1;
  }
  return { added, removed };
}
