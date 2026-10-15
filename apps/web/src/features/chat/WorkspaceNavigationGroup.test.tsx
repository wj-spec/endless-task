// @vitest-environment jsdom
/**
 * 批次 H / F-1：**已绑定但还没有对话的工作区是死角**。
 *
 * 用户实测：「新建的工作区没有『新建会话』按钮」。
 * 控制器取证的链条（`h-brief.md` §1①）：
 *  - `SessionRail.tsx` 的 `currentWorkspaceId` **优先取活动会话所属工作区**，
 *    所以只要还开着一条旧会话，新工作区永远不会是 current；
 *  - `WorkspaceNavigationGroup` 的行内新建按钮写着 `isCurrent && …` ⇒ 只给 current；
 *  - `WorkspaceSessionList` 的空态只渲染文字、**没给 `EmptyState` 的 `action`**；
 *  - 全局「新对话」按钮的 `workspaceCanCreate` 基于 current ⇒ 只会建到旧工作区。
 *
 * ⇒ 净效果：**该工作区的第一条对话没有任何入口能建出来**。
 * 本文件先钉住「必须有入口」这条对外行为（先红后绿），再钉住 F-2/F-3 的统一文案与形态。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { chatApi } from "./api";
import type { Conversation, Workspace } from "./apiTypes";
import { SessionRail } from "./SessionRail";
import { WorkspaceNavigationGroup } from "./WorkspaceNavigationGroup";
import type {
  SessionActions,
  WorkspaceNavigationGroup as WorkspaceGroupModel,
} from "./workspaceNavigationModel";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

/** 全批统一的动作名：同一个动作只留一个名字（F-2）。 */
const NEW_CONVERSATION_LABEL = "在此工作区新建对话";
/** 空态里的动作按钮文案（F-1② / F-2）。 */
const EMPTY_ACTION_LABEL = "新建对话";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  // jsdom 没有布局：SessionRail 会在 rAF 里调 `scrollIntoView`，不存在就是未捕获异常
  // （会污染测试结论）。与 recoveryAppWiring.test.tsx 同一配方。
  (Element.prototype as unknown as { scrollIntoView: () => void }).scrollIntoView =
    vi.fn();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.querySelectorAll(".workspace-hover-info").forEach((node) => node.remove());
  vi.restoreAllMocks();
});

const group = (
  over: Partial<WorkspaceGroupModel> = {},
): WorkspaceGroupModel => ({
  id: "ws-1",
  name: "工作区一",
  rootPath: "/tmp/ws1",
  createdAt: "2026-09-01T00:00:00Z",
  conversations: [],
  totalCount: 0,
  ...over,
});

/** 只带「新建对话」与「绑定目录」两个入口的最小动作集：其余缺席，用到即暴露。 */
const actionsWithNew = (
  onNewConversation: (workspaceId: string) => void,
): SessionActions =>
  ({
    onNewConversation,
    onBindWorkspace: () => {},
  }) as unknown as SessionActions;

type RenderOverrides = Omit<
  Parameters<typeof WorkspaceNavigationGroup>[0],
  "group" | "actions" | "currentWorkspaceId"
>;

const renderGroup = (
  model: WorkspaceGroupModel,
  opts: {
    onNewConversation?: (workspaceId: string) => void;
    isCurrent?: boolean;
    actions?: SessionActions;
  } = {},
) => {
  const { onNewConversation, isCurrent = false, actions } = opts;
  const base: RenderOverrides = {
    activeConversationId: null,
    homePath: "/tmp/home",
    open: true,
    showAll: false,
    renamingId: null,
    renameDraft: "",
    onToggleOpen: () => {},
    onToggleShowAll: () => {},
  };
  act(() =>
    root.render(
      <WorkspaceNavigationGroup
        {...base}
        actions={actions ?? actionsWithNew(onNewConversation ?? (() => {}))}
        currentWorkspaceId={isCurrent ? model.id : "ws-other"}
        group={model}
      />,
    ),
  );
};

const byLabel = (label: string) =>
  Array.from(
    container.querySelectorAll<HTMLButtonElement>(`button[aria-label="${label}"]`),
  );

describe("F-1 已绑定工作区必须有「新建对话」入口（先红后绿）", () => {
  it("非当前（已绑定）工作区也渲染行内新建按钮，点击带上**该工作区 id**", () => {
    const onNewConversation = vi.fn();
    renderGroup(group({ id: "ws-empty", name: "刚建的空白工作区" }), {
      isCurrent: false,
      onNewConversation,
    });

    // 死角复现点：旧实现写 `isCurrent && …`，非当前组**连按钮都没有**。
    const inline = byLabel(NEW_CONVERSATION_LABEL);
    expect(inline).toHaveLength(1);
    act(() => inline[0].click());
    expect(onNewConversation).toHaveBeenCalledTimes(1);
    expect(onNewConversation).toHaveBeenCalledWith("ws-empty");
  });

  it("空会话列表的空态给出动作按钮，点它建的就是该工作区的第一段对话", () => {
    const onNewConversation = vi.fn();
    renderGroup(group({ id: "ws-empty" }), {
      isCurrent: false,
      onNewConversation,
    });

    const empty = container.querySelector<HTMLElement>(".empty-state.rail-empty");
    expect(empty).not.toBeNull();
    const action = Array.from(
      empty!.querySelectorAll<HTMLButtonElement>("button"),
    ).find((node) => node.textContent?.trim() === EMPTY_ACTION_LABEL);
    expect(action).toBeDefined();
    act(() => action!.click());
    expect(onNewConversation).toHaveBeenCalledWith("ws-empty");
  });

  it("当前工作区同样有入口（不许为了修死角而删掉 current 的入口）", () => {
    renderGroup(group({ id: "ws-current" }), { isCurrent: true });
    expect(byLabel(NEW_CONVERSATION_LABEL)).toHaveLength(1);
  });

  it("未绑定目录的工作区不给新建入口（仍走「绑定目录…」）", () => {
    renderGroup(group({ id: "ws-unbound", rootPath: null }));
    expect(byLabel(NEW_CONVERSATION_LABEL)).toHaveLength(0);
    const bind = Array.from(
      container.querySelectorAll<HTMLButtonElement>("button"),
    ).filter((node) => node.textContent?.trim() === "绑定目录…");
    expect(bind.length).toBeGreaterThan(0);
  });
});

describe("F-2/F-3 同一动作一个名字、一种形态", () => {
  it("行内按钮与空态动作按钮使用同一条文案「新建对话」", () => {
    renderGroup(group({ id: "ws-empty2" }));
    const labels = Array.from(
      container.querySelectorAll<HTMLButtonElement>("button"),
    )
      .map((node) => node.textContent?.trim())
      .filter(Boolean);
    // 行内是纯图标按钮：可见文字只在 title 上，靠 aria-label 承担语义。
    expect(byLabel(NEW_CONVERSATION_LABEL)).toHaveLength(1);
    expect(labels).toContain(EMPTY_ACTION_LABEL);
    // 旧词必须绝迹（否则等于同一个动作两个名字）。
    expect(container.textContent ?? "").not.toContain("在当前工作区新建会话");
    expect(container.textContent ?? "").not.toContain("在这个工作区中新建一段对话");
    // 空态描述与按钮指向同一个动作（F-2 统一句式）。
    expect(container.textContent ?? "").toContain("在这里新建一段对话");
  });

  it("行内按钮与空态动作按钮同用 AddIcon（同一个图标语义）", () => {
    renderGroup(group({ id: "ws-empty3" }));
    const inline = byLabel(NEW_CONVERSATION_LABEL)[0];
    expect(inline.querySelector("svg")).not.toBeNull();
    const empty = container.querySelector<HTMLElement>(".empty-state.rail-empty")!;
    const action = Array.from(
      empty.querySelectorAll<HTMLButtonElement>("button"),
    ).find((node) => node.textContent?.trim() === EMPTY_ACTION_LABEL)!;
    expect(action.querySelector("svg")).not.toBeNull();
  });
});

/**
 * 控制器取证的「死角落」是**整条链**，不只是单个组件的渲染条件：
 * `SessionRail` 的 `currentWorkspaceId` **优先取活动会话所属工作区**，所以只要还开着
 * 一条旧会话，新工作区永远不会是 current。这里直接渲染真的 `SessionRail`
 * （真的 `useWorkspaceNavigation`，只把 `chatApi.listConversations` 打桩），
 * 钉住用户在实机上看到的那一幕：旧工作区有会话、新工作区为空 ⇒ 新工作区行上
 * **仍然要有**「新建对话」按钮，且按钮带的是**新工作区自己的 id**。
 */
const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
};

const railWorkspace = (
  id: string,
  name: string,
  rootPath: string | null,
): Workspace => ({
  id,
  name,
  rootPath,
  createdAt: "2026-09-01T00:00:00Z",
  updatedAt: "2026-09-01T00:00:00Z",
});

const railConversation = (
  id: string,
  workspaceId: string,
  title: string,
): Conversation => ({
  id,
  title,
  status: "active",
  nextTurnOrdinal: 1,
  titleIsManual: false,
  createdAt: "2026-09-01T00:00:00Z",
  updatedAt: "2026-09-02T00:00:00Z",
  archivedAt: null,
  parentConversationId: null,
  forkTurnId: null,
  kind: "normal",
  providerProfileId: null,
  modelOverride: null,
  promotedAt: null,
  workspaceId,
});

const renderRail = async (
  workspaces: Workspace[],
  conversationsByWorkspace: Record<string, Conversation[]>,
  onNewConversation: (workspaceId?: string) => void,
) => {
  vi.spyOn(chatApi, "listConversations").mockImplementation(
    async (_status, _query, workspaceId) =>
      conversationsByWorkspace[workspaceId ?? "__general__"] ?? [],
  );
  act(() =>
    root.render(
      <SessionRail
        activeConversationId="conv-old"
        collapsed={false}
        currentWorkspace={workspaces[0]}
        homePath="/tmp/home"
        onClose={() => {}}
        onChangeConversationStatus={() => {}}
        onCreateWorkspace={() => {}}
        onDeleteConversation={() => {}}
        onDeleteWorkspace={async () => {}}
        onNewConversation={onNewConversation}
        onOpenAssistant={() => {}}
        onOpenSettings={() => {}}
        onRenameConversation={() => {}}
        onRequestBindWorkspace={() => {}}
        onSearchChange={() => {}}
        onSelectConversation={() => {}}
        onStatusFilterChange={() => {}}
        open={false}
        pendingTotal={0}
        search=""
        statusFilter="active"
        workspaceCanCreate
        workspaceId="ws-old"
        workspaces={workspaces}
      />,
    ),
  );
  await flush();
};

describe("F-1 死角落·整条链（真的 SessionRail）", () => {
  it("已开着旧工作区的会话时，空的新工作区行上仍有「新建对话」，且 id 是新工作区的", async () => {
    const oldWs = railWorkspace("ws-old", "旧工作区", "/tmp/old");
    const emptyWs = railWorkspace("ws-empty", "刚建的空工作区", "/tmp/empty");
    const onNewConversation = vi.fn();

    await renderRail(
      [oldWs, emptyWs],
      { "ws-old": [railConversation("conv-old", "ws-old", "旧对话")] },
      onNewConversation,
    );

    // 旧工作区（= current，因为活动会话在它里面）有按钮。
    const oldGroup = container.querySelector<HTMLElement>(
      '[data-workspace-id="ws-old"]',
    )!;
    expect(
      oldGroup.querySelector(`button[aria-label="${NEW_CONVERSATION_LABEL}"]`),
    ).not.toBeNull();

    // 死角复现点：旧实现里这里**什么都没有** —— 新工作区永远不是 current。
    const emptyGroup = container.querySelector<HTMLElement>(
      '[data-workspace-id="ws-empty"]',
    )!;
    const emptyButton = emptyGroup.querySelector<HTMLButtonElement>(
      `button[aria-label="${NEW_CONVERSATION_LABEL}"]`,
    );
    expect(emptyButton).not.toBeNull();
    act(() => emptyButton!.click());
    expect(onNewConversation).toHaveBeenCalledWith("ws-empty");
  });

  it("空的新工作区展开后，空态里的「新建对话」也指向它自己", async () => {
    const oldWs = railWorkspace("ws-old", "旧工作区", "/tmp/old");
    const emptyWs = railWorkspace("ws-empty", "刚建的空工作区", "/tmp/empty");
    const onNewConversation = vi.fn();

    await renderRail(
      [oldWs, emptyWs],
      { "ws-old": [railConversation("conv-old", "ws-old", "旧对话")] },
      onNewConversation,
    );

    const emptyGroup = container.querySelector<HTMLElement>(
      '[data-workspace-id="ws-empty"]',
    )!;
    const toggle = emptyGroup.querySelector<HTMLButtonElement>(
      ".workspace-item-main",
    )!;
    act(() => toggle.click());

    const action = Array.from(
      emptyGroup.querySelectorAll<HTMLButtonElement>(".rail-empty-action"),
    ).find((node) => node.textContent?.trim() === EMPTY_ACTION_LABEL);
    expect(action).toBeDefined();
    act(() => action!.click());
    expect(onNewConversation).toHaveBeenCalledWith("ws-empty");
  });
});
