// @vitest-environment jsdom
/**
 * C3b：`@` 引用候选浮层**在真实挂载链上**的两处都接上了。
 *
 * 为什么这份测试在 `ChatWorkSurface` 这一层而不是只测 `ChatComposer`：
 * `/技能名` 曾经出过一次"侧边面板的 composer 没拿到 skillCandidates"的事故——
 * 侧栏输入 `/` 看到的是"还没有可调用的技能"的空态（列表其实有几十条）。今天
 * `ChatWorkSurface.tsx` 只有**一处** `<ChatComposer>`，靠 `sideMode` 同时服务
 * 主区与侧区，所以"漏接"的形态变成"同一个挂载点漏传一个 prop"——只在 composer
 * 内部渲染的测试**看不见**这件事。这里从工作面往下渲染，`sideMode` 两种取值各
 * 一条（外加主区一条），断言候选浮层真的出现。
 *
 * 断言仍然落在**真实依赖**上：`chatApi.listConversations` 用 `vi.spyOn`
 * （真模块 + 打桩方法），不做 `vi.mock("./api")` 的整模块替换。
 */
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { chatApi } from "./api";
import { ChatWorkSurface } from "./ChatWorkSurface";
import type {
  Conversation,
  ConversationSnapshot,
  RuntimeV2ConversationReference,
  RuntimeV2Snapshot,
} from "./apiTypes";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const SENDING = "conv-c3b-surface-self";
const OTHER = "conv-c3b-surface-other";

const conversation: Conversation = {
  id: SENDING,
  title: "正在发送的会话",
  status: "active",
  nextTurnOrdinal: 3,
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
  workspaceId: "ws-c3b",
};

const other: Conversation = {
  ...conversation,
  id: OTHER,
  title: "另一个会话",
  workspaceId: "ws-c3b",
};

const snapshot: ConversationSnapshot = {
  conversation,
  turns: [],
  files: [],
};

/** 只填 `@` 分支真正读到的字段；其余用 `as` 声明为完整快照。 */
const runtimeSnapshot = (mainLaneId: string | null): RuntimeV2Snapshot =>
  ({
    snapshotVersion: 1,
    conversationId: SENDING,
    activeLaneId: mainLaneId,
    mainLaneId,
    runningLaneId: null,
    runningRunId: null,
    activeRunId: null,
    activeRunVariantId: null,
    lastEventSeq: 0,
    entries: [],
    runState: null,
    pendingApprovals: [],
    toolStates: [],
    contextUsage: { inputTokens: 0, outputTokens: 0 },
    interruptedRuns: [],
    stuck: null,
    escalation: null,
    verification: null,
    usage: null,
    capabilities: [],
  }) as RuntimeV2Snapshot;

type SurfaceProps = Parameters<typeof ChatWorkSurface>[0];

let container: HTMLDivElement;
let root: Root;
let draft = "";

/** 把草稿推到受控 textarea 里（React 拦直接赋值，走原型 setter）。 */
const type = (value: string) => {
  draft = value;
  const node = container.querySelector<HTMLTextAreaElement>("textarea")!;
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  setter?.call(node, value);
  node.setSelectionRange(value.length, value.length);
  act(() => {
    node.dispatchEvent(new Event("input", { bubbles: true }));
  });
};

const renderSurface = (
  sideMode: "temporary_conversation" | "branch_lane" | null,
  variant: "main" | "side",
  laneId: string | null,
  onSend: (references: RuntimeV2ConversationReference[]) => void = () => undefined,
) => {
  const element = (): SurfaceProps =>
    ({
      conversation: snapshot,
      draft,
      error: null,
      health: null,
      isGenerating: false,
      liveTurns: {},
      loading: false,
      pendingAction: null,
      providers: [],
      runtimeEvents: [],
      runtimeSnapshot: runtimeSnapshot(laneId),
      currentLaneId: laneId,
      sideMode,
      variant,
      onArchive: () => undefined,
      onCancel: () => undefined,
      onDelete: () => undefined,
      onDismissError: () => undefined,
      onDraftChange: (value: string) => {
        draft = value;
        act(() => root.render(<ChatWorkSurface {...element()} />));
      },
      onMenu: () => undefined,
      onPromote: () => undefined,
      onRegenerate: () => undefined,
      onResolveApproval: () => undefined,
      onRemoveFile: () => undefined,
      onRename: () => undefined,
      onRestore: () => undefined,
      onRetry: () => undefined,
      onSelectVariant: () => undefined,
      onSend,
      onModelChange: () => undefined,
      onUploadFile: () => undefined,
      proposalBusyId: null,
      proposalErrors: {},
      resolvedArtifacts: {},
      turnProposals: () => ({
        artifacts: [],
        memories: [],
        tasks: [],
        knowledge: [],
      }),
      onResolveArtifactProposal: () => undefined,
      onResolveKnowledgeProposal: () => undefined,
      onResolveMemoryProposal: () => undefined,
      onResolveTaskProposal: () => undefined,
    }) as unknown as SurfaceProps;
  act(() => root.render(<ChatWorkSurface {...element()} />));
};

const press = (key: string) =>
  act(() => {
    container.querySelector<HTMLTextAreaElement>("textarea")!.dispatchEvent(
      new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }),
    );
  });

const mentionMenu = () =>
  document.querySelector<HTMLUListElement>("ul.composer-mention-menu");

const mentionOptions = () =>
  Array.from(document.querySelectorAll(".composer-mention-menu button")).map(
    (node) => node.querySelector(".composer-mention-name")?.textContent,
  );

let listSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  draft = "";
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  listSpy = vi
    .spyOn(chatApi, "listConversations")
    .mockResolvedValue([conversation, other]);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("ChatWorkSurface 的 `@` 引用候选（C3b：sideMode 两种取值都要能开）", () => {
  it("侧区 · branch_lane：候选浮层出现，且不列出正在发送的这个会话", async () => {
    renderSurface("branch_lane", "side", "lane-c3b");
    type("@");
    await act(async () => {
      await Promise.resolve();
    });
    expect(listSpy).toHaveBeenCalledWith("active", undefined, "ws-c3b");
    expect(mentionMenu()).not.toBeNull();
    expect(mentionOptions()).toEqual(["另一个会话"]);
  });

  it("侧区 · temporary_conversation：候选浮层出现（同一个挂载点、另一种取值）", async () => {
    renderSurface("temporary_conversation", "side", "lane-temp-c3b");
    type("@");
    await act(async () => {
      await Promise.resolve();
    });
    expect(mentionMenu()).not.toBeNull();
    expect(mentionOptions()).toEqual(["另一个会话"]);
  });

  it("主区：候选浮层出现（同一挂载点的第三条证人）", async () => {
    renderSurface(null, "main", "lane-c3b");
    type("@");
    await act(async () => {
      await Promise.resolve();
    });
    expect(mentionMenu()).not.toBeNull();
  });

  it("侧区 · branch_lane：发送时把引用交给 onSend（工作面这一层不吞掉它）", async () => {
    const sent: RuntimeV2ConversationReference[][] = [];
    renderSurface("branch_lane", "side", "lane-c3b", (references) =>
      sent.push(references),
    );
    type("@");
    await act(async () => {
      await Promise.resolve();
    });
    act(() => {
      document
        .querySelector<HTMLButtonElement>(".composer-mention-menu button")!
        .click();
    });
    await act(async () => {
      await Promise.resolve();
    });
    type("把这个会话的结论也带上");
    press("Enter");
    expect(sent).toEqual([
      [
        {
          kind: "conversation",
          conversationId: OTHER,
          mode: "summary",
          entryIds: [],
        },
      ],
    ]);
  });

  it("没有可挂载 lane 的会话：给明确说明、候选为空，且一个请求都不发", async () => {
    renderSurface(null, "main", null);
    type("@");
    await act(async () => {
      await Promise.resolve();
    });
    // 锚点门禁走的是工作面传下来的 `mentionAnchorLaneId`（= 快照的 mainLaneId）：
    // 这个 prop 一旦在主/侧任一挂载点上漏传，上面三条会"看起来正常"（未知 ⇒ 不拦），
    // 而这一条会红——这正是当年 `skillCandidates` 漏接的同一形态。
    expect(listSpy).not.toHaveBeenCalled();
    expect(mentionMenu()).toBeNull();
    const empty = document.querySelector(".composer-mention-menu.is-empty");
    expect(empty).not.toBeNull();
    expect(empty!.textContent).toContain("还没有可挂载引用的上下文");
  });
});
