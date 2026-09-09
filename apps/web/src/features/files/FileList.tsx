import type { KeyboardEvent } from "react";
import type { WorkspaceTreeEntry } from "../chat/apiTypes";
import { FileTypeIcon, categoryOfEntry } from "./FileTypeIcon";
import { formatFileSize, isNoisyEntry } from "./workspaceFiles";

export type FileListProps = {
  entries: WorkspaceTreeEntry[];
  busy: boolean;
  error: string | null;
  truncated: boolean;
  hideNoisy: boolean;
  onOpenDirectory: (path: string) => void;
  onOpenFile: (path: string) => void;
};

const activate = (event: KeyboardEvent<HTMLLIElement>, action: () => void) => {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  action();
};

/**
 * S14：目录标签内容——**同一级平铺**列出该目录的直接子项。
 *
 * 不做多级缩进；深入目录 = 打开新标签，靠标签与面包屑导航。
 */
export function FileList({
  entries,
  busy,
  error,
  truncated,
  hideNoisy,
  onOpenDirectory,
  onOpenFile,
}: FileListProps) {
  if (busy) return <p className="file-tree-note">正在读取…</p>;
  if (error) {
    return (
      <p className="file-tree-error" role="alert">
        {error}
      </p>
    );
  }
  if (entries.length === 0) {
    return <p className="file-tree-note">空目录</p>;
  }

  return (
    <ul aria-label="目录内容" className="file-list">
      {entries.map((entry) => {
        const isDirectory = entry.kind === "directory";
        if (hideNoisy && isNoisyEntry(entry)) return null;
        const open = () =>
          isDirectory ? onOpenDirectory(entry.relativePath) : onOpenFile(entry.relativePath);
        return (
          <li
            className={["file-list-item", isDirectory ? "is-directory" : "is-file"]
              .filter(Boolean)
              .join(" ")}
            key={entry.relativePath}
            onClick={open}
            onKeyDown={(event) => activate(event, open)}
            role="button"
            tabIndex={0}
            title={entry.relativePath}
          >
            <FileTypeIcon category={categoryOfEntry(entry)} />
            <span className="file-list-name">{entry.name}</span>
            <span className="file-list-meta">
              {isDirectory ? "目录" : formatFileSize(entry)}
            </span>
          </li>
        );
      })}
      {truncated ? (
        <li className="file-list-note">目录条目过多，仅显示前 200 项。</li>
      ) : null}
    </ul>
  );
}
