import type { WorkspaceTreeEntry } from "../chat/apiTypes";
import { extensionOf } from "./workspaceFiles";

/** S14：文件按分类展示图标（目录 / 文档 / 代码 / 数据 / 图片 / 文本 / 其他）。 */
export type FileCategory =
  | "folder"
  | "markdown"
  | "code"
  | "data"
  | "image"
  | "text"
  | "other";

const CODE_EXTENSIONS = new Set([
  "c",
  "cpp",
  "cs",
  "css",
  "go",
  "h",
  "html",
  "java",
  "js",
  "jsx",
  "kt",
  "lua",
  "php",
  "py",
  "rb",
  "rs",
  "scss",
  "sh",
  "sql",
  "swift",
  "ts",
  "tsx",
  "vue",
  "zsh",
]);

const DATA_EXTENSIONS = new Set([
  "csv",
  "ini",
  "json",
  "lock",
  "toml",
  "xml",
  "yaml",
  "yml",
]);

const IMAGE_EXTENSIONS = new Set([
  "bmp",
  "gif",
  "ico",
  "jpeg",
  "jpg",
  "png",
  "svg",
  "webp",
]);

const TEXT_EXTENSIONS = new Set(["log", "md", "mdx", "rst", "text", "txt"]);

export function categoryOfPath(path: string, kind?: "file" | "directory"): FileCategory {
  if (kind === "directory") return "folder";
  const extension = extensionOf(path);
  if (!extension) return "other";
  if (extension === "md" || extension === "mdx") return "markdown";
  if (CODE_EXTENSIONS.has(extension)) return "code";
  if (DATA_EXTENSIONS.has(extension)) return "data";
  if (IMAGE_EXTENSIONS.has(extension)) return "image";
  if (TEXT_EXTENSIONS.has(extension)) return "text";
  return "other";
}

export function categoryOfEntry(entry: WorkspaceTreeEntry): FileCategory {
  return categoryOfPath(entry.relativePath, entry.kind);
}

const PATHS: Record<Exclude<FileCategory, "folder">, string> = {
  markdown: "M4 4h9l4 4v12H4z M13 4v4h4",
  code: "m9 8-4 4 4 4 M15 8l4 4-4 4",
  data: "M8 5c-2 0-3 1-3 3v2c0 1-.7 2-2 2 1.3 0 2 1 2 2v2c0 2 1 3 3 3 M16 5c2 0 3 1 3 3v2c0 1 .7 2 2 2-1.3 0-2 1-2 2v2c0 2-1 3-3 3",
  image: "M4 5h16v14H4z M4 15l5-4 4 3 3-3 4 4",
  text: "M5 6h14M5 10h14M5 14h10M5 18h7",
  other: "M7 3h7l4 4v14H7z M14 3v5h5",
};

export type FileTypeIconProps = {
  category: FileCategory;
  size?: number;
  className?: string;
};

/** 分类图标：单一 stroke 风格，颜色由 CSS 类决定（`.file-icon.is-<category>`）。 */
export function FileTypeIcon({ category, size = 14, className }: FileTypeIconProps) {
  if (category === "folder") {
    return (
      <svg
        aria-hidden="true"
        className={["file-icon", "is-folder", className].filter(Boolean).join(" ")}
        fill="none"
        height={size}
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.6"
        viewBox="0 0 24 24"
        width={size}
      >
        <path d="M3 6h7l2 2h9v11H3z" />
      </svg>
    );
  }
  return (
    <svg
      aria-hidden="true"
      className={["file-icon", `is-${category}`, className].filter(Boolean).join(" ")}
      fill="none"
      height={size}
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="1.6"
      viewBox="0 0 24 24"
      width={size}
    >
      <path d={PATHS[category]} />
    </svg>
  );
}
