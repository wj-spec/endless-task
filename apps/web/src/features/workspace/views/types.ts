/** S10：右侧边栏的视图类型与注册表（每个类型单实例，终端内部再分会话页签）。 */

export type WorkspaceViewKind = "artifacts" | "files" | "terminals" | "browser";

export type WorkspaceView = {
  kind: WorkspaceViewKind;
  title: string;
  /** 副标题（面板头部说明）。 */
  subtitle: string;
};

export const VIEW_LABELS: Record<WorkspaceViewKind, string> = {
  artifacts: "产物",
  files: "文件",
  terminals: "终端",
  browser: "浏览器",
};

export const VIEW_SUBTITLES: Record<WorkspaceViewKind, string> = {
  artifacts: "此会话生成、可版本回滚的文档",
  files: "浏览与预览绑定的本地目录",
  terminals: "在工作区目录运行持久终端会话",
  browser: "网页浏览能力规划中",
};

/** 「+」菜单顺序：产物 → 文件系统 → 终端 → 浏览器。 */
export const VIEW_MENU: WorkspaceViewKind[] = [
  "artifacts",
  "files",
  "terminals",
  "browser",
];

export const VIEW_HINTS: Record<WorkspaceViewKind, string> = {
  artifacts: "对话产出的文档与提案",
  files: "本地目录树、预览与编辑",
  terminals: "持久 PTY 会话（命令逐条确认）",
  browser: "占位视图，能力后续排期",
};

export function viewOf(kind: WorkspaceViewKind): WorkspaceView {
  return { kind, title: VIEW_LABELS[kind], subtitle: VIEW_SUBTITLES[kind] };
}

export function isWorkspaceViewKind(value: unknown): value is WorkspaceViewKind {
  return (
    value === "artifacts" ||
    value === "files" ||
    value === "terminals" ||
    value === "browser"
  );
}
