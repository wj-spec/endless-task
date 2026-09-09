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
