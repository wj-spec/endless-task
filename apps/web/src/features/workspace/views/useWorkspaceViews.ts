import { useCallback, useEffect, useMemo, useState } from "react";
import {
  isWorkspaceViewKind,
  VIEW_MENU,
  viewOf,
  type WorkspaceView,
  type WorkspaceViewKind,
} from "./types";

const STORAGE_PREFIX = "endless-task-workspace-views:";
const DEFAULT_KIND: WorkspaceViewKind = "artifacts";

export type FileTarget = { path: string; nonce: number } | null;

export type WorkspaceViewsController = {
  views: WorkspaceView[];
  active: WorkspaceView;
  activeKind: WorkspaceViewKind;
  open: (kind: WorkspaceViewKind) => void;
  close: (kind: WorkspaceViewKind) => void;
  activate: (kind: WorkspaceViewKind) => void;
  /** 打开文件视图并定位到某个路径（产物 → 文件交叉跳转）。 */
  openFile: (path: string) => void;
  fileTarget: FileTarget;
  clearFileTarget: () => void;
};

type Persisted = { open: WorkspaceViewKind[]; active: WorkspaceViewKind | null };

/** 解析持久化状态（纯函数，便于单测）。 */
export function parsePersisted(raw: string | null): Persisted {
  const fallback: Persisted = { open: [DEFAULT_KIND], active: DEFAULT_KIND };
  if (!raw) return fallback;
  try {
    const parsed = JSON.parse(raw) as Partial<Persisted>;
    const open = (parsed.open ?? []).filter(isWorkspaceViewKind);
    if (open.length === 0) return fallback;
    const active =
      parsed.active && open.includes(parsed.active) ? parsed.active : open[0];
    return { open, active };
  } catch {
    return fallback;
  }
}

const readPersisted = (conversationId: string): Persisted => {
  if (typeof window === "undefined" || !conversationId) {
    return { open: [DEFAULT_KIND], active: DEFAULT_KIND };
  }
  return parsePersisted(
    localStorage.getItem(`${STORAGE_PREFIX}${conversationId}`),
  );
};

/**
 * S10：右侧边栏视图（标签页）状态。
 *
 * 每个类型单实例；「+」新建即激活（已打开则切过去）。按会话记忆，
 * 刷新/切换会话后恢复上次打开的视图与激活项。
 */
export function useWorkspaceViews(conversationId: string): WorkspaceViewsController {
  const initial = useMemo(() => readPersisted(conversationId), [conversationId]);
  const [openKinds, setOpenKinds] = useState<WorkspaceViewKind[]>(initial.open);
  const [activeKind, setActiveKind] = useState<WorkspaceViewKind>(
    initial.active ?? DEFAULT_KIND,
  );
  const [fileTarget, setFileTarget] = useState<FileTarget>(null);

  // 切换会话时重置为该会话的记忆。
  useEffect(() => {
    const restored = readPersisted(conversationId);
    setOpenKinds(restored.open);
    setActiveKind(restored.active ?? DEFAULT_KIND);
    setFileTarget(null);
  }, [conversationId]);

  useEffect(() => {
    if (typeof window === "undefined" || !conversationId) return;
    localStorage.setItem(
      `${STORAGE_PREFIX}${conversationId}`,
      JSON.stringify({ open: openKinds, active: activeKind }),
    );
  }, [activeKind, conversationId, openKinds]);

  const open = useCallback((kind: WorkspaceViewKind) => {
    setOpenKinds((current) =>
      current.includes(kind) ? current : [...VIEW_MENU.filter((item) => current.includes(item) || item === kind)],
    );
    setActiveKind(kind);
  }, []);

  const close = useCallback((kind: WorkspaceViewKind) => {
    setOpenKinds((current) => {
      const next = current.filter((item) => item !== kind);
      const fallback = next[0] ?? DEFAULT_KIND;
      setActiveKind((active) => (active === kind ? fallback : active));
      return next.length > 0 ? next : [DEFAULT_KIND];
    });
  }, []);

  const openFile = useCallback(
    (path: string) => {
      setFileTarget({ path, nonce: Date.now() });
      open("files");
    },
    [open],
  );

  const views = useMemo(
    () => VIEW_MENU.filter((kind) => openKinds.includes(kind)).map(viewOf),
    [openKinds],
  );

  return {
    views,
    active: viewOf(activeKind),
    activeKind,
    open,
    close,
    activate: setActiveKind,
    openFile,
    fileTarget,
    clearFileTarget: () => setFileTarget(null),
  };
}
