import { useCallback, useEffect, useRef, useState } from "react";
import { chatApi } from "../chat/api";
import type { WorkspaceTreeEntry } from "../chat/apiTypes";

type TreeState = {
  children: Record<string, WorkspaceTreeEntry[]>;
  truncated: Record<string, boolean>;
};

const EMPTY_STATE: TreeState = { children: {}, truncated: {} };

export type WorkspaceTreeController = {
  children: Record<string, WorkspaceTreeEntry[]>;
  truncated: Record<string, boolean>;
  expanded: string[];
  loading: string[];
  errors: Record<string, string>;
  rootBusy: boolean;
  toggle: (path: string) => void;
  /** 保留展开状态，只重取已加载层级（窗口聚焦 / 手动刷新）。 */
  refresh: () => void;
  /** 整棵树重建并折叠到根（切换隐藏文件开关等场景）。 */
  hardRefresh: () => void;
};

/**
 * P0 文件面板：按层懒加载的工作区目录树。
 *
 * 每层只请求一次并缓存；切换工作区或「显示隐藏文件」时整棵树失效重建。
 * 同一层重复请求会被忽略（loading 集合去重），避免展开/折叠抖动。
 */
export function useWorkspaceTree(
  workspaceId: string,
  showHidden: boolean,
): WorkspaceTreeController {
  const [state, setState] = useState<TreeState>(EMPTY_STATE);
  const [expanded, setExpanded] = useState<string[]>([]);
  const [loading, setLoading] = useState<string[]>([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const inFlight = useRef<Set<string>>(new Set());
  const generation = useRef(0);

  const load = useCallback(
    (path: string) => {
      if (inFlight.current.has(path)) return;
      inFlight.current.add(path);
      const currentGeneration = generation.current;
      setLoading((previous) => (previous.includes(path) ? previous : [...previous, path]));
      setErrors((previous) => {
        if (!(path in previous)) return previous;
        const next = { ...previous };
        delete next[path];
        return next;
      });
      void chatApi
        .listWorkspaceTree(workspaceId, path, showHidden)
        .then((listing) => {
          if (generation.current !== currentGeneration) return;
          setState((previous) => ({
            children: { ...previous.children, [path]: listing.entries },
            truncated: { ...previous.truncated, [path]: listing.truncated },
          }));
        })
        .catch(() => {
          if (generation.current !== currentGeneration) return;
          setErrors((previous) => ({ ...previous, [path]: "目录加载失败。" }));
        })
        .finally(() => {
          inFlight.current.delete(path);
          if (generation.current !== currentGeneration) return;
          setLoading((previous) => previous.filter((item) => item !== path));
        });
    },
    [showHidden, workspaceId],
  );

  const refresh = useCallback(() => {
    generation.current += 1;
    inFlight.current.clear();
    setState(EMPTY_STATE);
    setExpanded([]);
    setLoading([]);
    setErrors({});
    load("");
  }, [load]);

  useEffect(() => {
    generation.current += 1;
    inFlight.current.clear();
    setState(EMPTY_STATE);
    setExpanded([]);
    setLoading([]);
    setErrors({});
    load("");
  }, [load]);

  // 保留展开状态的刷新：只重取已加载过的层级（窗口重新获得焦点时调用）。
  const stateRef = useRef(state);
  stateRef.current = state;

  const reloadLoaded = useCallback(() => {
    const loaded = Object.keys(stateRef.current.children);
    for (const path of loaded.length > 0 ? loaded : [""]) load(path);
  }, [load]);

  useEffect(() => {
    const onFocus = () => reloadLoaded();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [reloadLoaded]);

  const toggle = useCallback(
    (path: string) => {
      setExpanded((previous) => {
        if (previous.includes(path)) {
          return previous.filter((item) => item !== path);
        }
        return [...previous, path];
      });
      if (!state.children[path]) load(path);
    },
    [load, state.children],
  );

  return {
    children: state.children,
    truncated: state.truncated,
    expanded,
    loading,
    errors,
    rootBusy: loading.includes("") && !state.children[""],
    toggle,
    refresh: reloadLoaded,
    hardRefresh: refresh,
  };
}
