import { useEffect, useMemo, useRef, useState } from "react";
import type { RuntimeV2Lane } from "./apiTypes";
import { RowMenu } from "../ui/RowMenu";

type BranchNavigatorProps = {
  conversationId: string;
  lanes: RuntimeV2Lane[];
  currentLaneId: string | null;
  disabled: boolean;
  onArchive: (laneId: string, includeArchived: boolean) => void;
  onOpenSide: (laneId: string) => void;
  onPromote: (laneId: string) => void;
  onRename: (laneId: string, displayName: string | null) => void;
  onRestore: (laneId: string) => void;
  onShowArchived: (visible: boolean) => void | Promise<void>;
  onSwitch: (laneId: string) => void;
};

type LaneRow = {
  lane: RuntimeV2Lane;
  depth: number;
};

const laneTitle = (lane: RuntimeV2Lane) =>
  lane.displayName ??
  lane.title ??
  lane.baseEntryExcerpt ??
  (lane.isMain ? "当前主线" : "未命名分支");

const isLaneArchived = (lane: RuntimeV2Lane) =>
  lane.status === "archived" || lane.archived;

const orderLanes = (lanes: RuntimeV2Lane[]): LaneRow[] => {
  const sorted = [...lanes].sort((left, right) =>
    left.createdAt.localeCompare(right.createdAt),
  );
  const byId = new Map(sorted.map((lane) => [lane.id, lane]));
  const children = new Map<string | null, RuntimeV2Lane[]>();

  for (const lane of sorted) {
    const parentId =
      lane.sourceLaneId && byId.has(lane.sourceLaneId)
        ? lane.sourceLaneId
        : null;
    const siblings = children.get(parentId) ?? [];
    siblings.push(lane);
    children.set(parentId, siblings);
  }

  const rows: LaneRow[] = [];
  const visited = new Set<string>();
  const visit = (lane: RuntimeV2Lane, depth: number) => {
    if (visited.has(lane.id)) return;
    visited.add(lane.id);
    rows.push({ lane, depth });
    for (const child of children.get(lane.id) ?? []) visit(child, depth + 1);
  };

  for (const root of children.get(null) ?? []) visit(root, 0);
  for (const lane of sorted) visit(lane, 0);
  return rows;
};

export function BranchNavigator({
  conversationId,
  lanes,
  currentLaneId,
  disabled,
  onArchive,
  onOpenSide,
  onPromote,
  onRename,
  onRestore,
  onShowArchived,
  onSwitch,
}: BranchNavigatorProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [showArchived, setShowArchived] = useState(false);
  const [renamingLaneId, setRenamingLaneId] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");
  const rows = useMemo(() => orderLanes(lanes), [lanes]);
  const currentLane =
    lanes.find((lane) => lane.id === currentLaneId) ??
    lanes.find((lane) => lane.isMain) ??
    null;

  useEffect(() => {
    setOpen(false);
    setShowArchived(false);
    setRenamingLaneId(null);
  }, [conversationId]);

  useEffect(() => {
    if (!open) return;
    const closeOnPointerDown = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
        setRenamingLaneId(null);
        if (showArchived) {
          setShowArchived(false);
          void onShowArchived(false);
        }
      }
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      setOpen(false);
      setRenamingLaneId(null);
      if (showArchived) {
        setShowArchived(false);
        void onShowArchived(false);
      }
    };
    document.addEventListener("mousedown", closeOnPointerDown);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnPointerDown);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [onShowArchived, open, showArchived]);

  const closePopover = () => {
    setOpen(false);
    setRenamingLaneId(null);
    if (showArchived) {
      setShowArchived(false);
      void onShowArchived(false);
    }
  };

  const commitRename = (lane: RuntimeV2Lane) => {
    const value = renameDraft.trim();
    onRename(lane.id, value || null);
    setRenamingLaneId(null);
  };

  const hasBranch = lanes.length > 1 && lanes.some((lane) => !lane.isMain);
  if (!hasBranch || !currentLane) return null;

  return (
    <div className="branch-navigator" ref={rootRef}>
      <button
        aria-expanded={open}
        className="branch-navigator-trigger"
        onClick={() => {
          if (open) closePopover();
          else setOpen(true);
        }}
        type="button"
      >
        <span>{currentLane.isMain ? "主线" : "分支"}</span>
        <strong>{laneTitle(currentLane)}</strong>
        <span aria-hidden="true">⌄</span>
      </button>

      {open ? (
        <div className="branch-navigator-popover">
          <div className="branch-navigator-toolbar">
            <strong>会话路径</strong>
            <span>从已完成回答创建分支</span>
          </div>

          <div className="branch-navigator-tree">
            {rows.map(({ lane, depth }) => {
              const selected = lane.id === currentLane.id;
              const laneArchived = isLaneArchived(lane);
              return (
                <div
                  className={`branch-navigator-row${selected ? " is-selected" : ""}${
                    laneArchived ? " is-archived" : ""
                  }`}
                  key={lane.id}
                  style={{ paddingLeft: 10 + depth * 16 }}
                >
                  {renamingLaneId === lane.id ? (
                    <input
                      aria-label="分支名称"
                      autoFocus
                      onBlur={() => commitRename(lane)}
                      onChange={(event) => setRenameDraft(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          event.preventDefault();
                          event.currentTarget.blur();
                        }
                        if (event.key === "Escape") {
                          event.preventDefault();
                          setRenamingLaneId(null);
                        }
                      }}
                      value={renameDraft}
                    />
                  ) : (
                    <button
                      className="branch-navigator-select"
                      disabled={disabled || laneArchived}
                      onClick={() => {
                        onSwitch(lane.id);
                        closePopover();
                      }}
                      type="button"
                    >
                      <span>{lane.isMain ? "主线" : laneArchived ? "已归档" : "分支"}</span>
                      <strong>{laneTitle(lane)}</strong>
                    </button>
                  )}

                  {!lane.isMain ? (
                    <RowMenu
                      disabled={disabled}
                      items={[
                        {
                          label: "重命名",
                          disabled: disabled || laneArchived,
                          onSelect: () => {
                            setRenameDraft(lane.displayName ?? lane.title ?? "");
                            setRenamingLaneId(lane.id);
                          },
                        },
                        {
                          label: "右侧对照",
                          disabled: disabled || laneArchived,
                          onSelect: () => {
                            onOpenSide(lane.id);
                            closePopover();
                          },
                        },
                        {
                          label: "设为主线",
                          disabled: disabled || laneArchived,
                          onSelect: () => {
                            onPromote(lane.id);
                            closePopover();
                          },
                        },
                        laneArchived
                          ? {
                              label: "恢复分支",
                              disabled,
                              onSelect: () => {
                                onRestore(lane.id);
                                closePopover();
                              },
                            }
                          : {
                              label: "归档分支",
                              disabled,
                              danger: true,
                              onSelect: () => {
                                onArchive(lane.id, showArchived);
                                closePopover();
                              },
                            },
                      ]}
                      triggerAriaLabel={`管理分支：${laneTitle(lane)}`}
                      triggerClassName="branch-navigator-menu"
                    />
                  ) : null}
                </div>
              );
            })}
          </div>

          <label className="branch-navigator-archived">
            <input
              checked={showArchived}
              disabled={disabled}
              onChange={(event) => {
                const visible = event.target.checked;
                setShowArchived(visible);
                void onShowArchived(visible);
              }}
              type="checkbox"
            />
            显示已归档分支
          </label>
        </div>
      ) : null}
    </div>
  );
}