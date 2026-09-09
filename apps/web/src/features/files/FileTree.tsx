import type { KeyboardEvent } from "react";
import type { WorkspaceTreeEntry } from "../chat/apiTypes";
import { ChevronIcon } from "../ui/Icons";
import { formatFileSize, isNoisyEntry } from "./workspaceFiles";

export type FileTreeProps = {
  children: Record<string, WorkspaceTreeEntry[]>;
  expanded: string[];
  loading: string[];
  errors: Record<string, string>;
  truncated: Record<string, boolean>;
  onToggle: (path: string) => void;
  onOpen: (path: string) => void;
};

type LevelProps = FileTreeProps & {
  parentPath: string;
  depth: number;
};

function activate(event: KeyboardEvent<HTMLLIElement>, action: () => void): void {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  action();
}

function FileTreeLevel(props: LevelProps) {
  const entries = props.children[props.parentPath] ?? [];
  const busy = props.loading.includes(props.parentPath);
  const error = props.errors[props.parentPath];
  const truncated = props.truncated[props.parentPath] === true;

  return (
    <>
      {busy ? <p className="file-tree-note">正在读取…</p> : null}
      {error ? (
        <p className="file-tree-error" role="alert">
          {error}
        </p>
      ) : null}
      {entries.length === 0 && !busy && !error ? (
        <p className="file-tree-note">空目录</p>
      ) : null}
      <ul className="file-tree-group" role="group">
        {entries.map((entry) => {
          const path = entry.relativePath;
          const isDirectory = entry.kind === "directory";
          const isExpanded = props.expanded.includes(path);
          const classes = [
            "file-tree-item",
            isDirectory ? "is-directory" : "is-file",
            isNoisyEntry(entry) ? "is-noisy" : "",
          ]
            .filter(Boolean)
            .join(" ");
          return (
            <li
              aria-expanded={isDirectory ? isExpanded : undefined}
              aria-level={props.depth + 1}
              className={classes}
              key={path}
              onClick={() => (isDirectory ? props.onToggle(path) : props.onOpen(path))}
              onKeyDown={(event) =>
                activate(event, () =>
                  isDirectory ? props.onToggle(path) : props.onOpen(path),
                )
              }
              role="treeitem"
              tabIndex={0}
            >
              <span
                className="file-tree-row"
                style={{ paddingLeft: `${8 + props.depth * 14}px` }}
              >
                {isDirectory ? (
                  <ChevronIcon
                    className="file-tree-chevron"
                    direction={isExpanded ? "down" : "right"}
                    size={12}
                  />
                ) : (
                  <span aria-hidden="true" className="file-tree-dot" />
                )}
                <span className="file-tree-name">{entry.name}</span>
                <span className="file-tree-size">{formatFileSize(entry)}</span>
              </span>
              {isDirectory && isExpanded ? (
                <FileTreeLevel
                  {...props}
                  depth={props.depth + 1}
                  parentPath={path}
                />
              ) : null}
            </li>
          );
        })}
      </ul>
      {truncated ? (
        <p className="file-tree-note">目录条目过多，仅显示前 200 项。</p>
      ) : null}
    </>
  );
}

/** P0 文件面板：工作区目录树（懒加载、键盘可操作）。 */
export function FileTree(props: FileTreeProps) {
  return (
    <div aria-label="工作区文件" className="file-tree" role="tree">
      <FileTreeLevel {...props} depth={0} parentPath="" />
    </div>
  );
}
