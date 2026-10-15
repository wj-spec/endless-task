// @vitest-environment jsdom
/**
 * C3b 修复波（C-1）：**真 App 端到端**的 `@` 引用接线守护。
 *
 * 为什么必须有这份文件（复审 C-1 的决定性证据）：`references` 的链路里曾有
 * **3 段"改坏了也不红"**——
 *   ① `App.tsx` 主区转发（`onSend={(references) => void chat.send(references)}`）
 *   ② `App.tsx` 侧区转发（`onSend={(references) => void chat.sendSide(references)}`）
 *   ③ `useChatApplication.ts` 里 hook → `chatApi.createRuntimeV2Message` 的实参
 * 把 ①② 同时掐断、或把 ③ 少传一个实参，`test:unit` 仍然 **82 files / 634 passed**、
 * `npm run build` 仍然 EXIT=0 ⇒ **整条特性可以静默失效**（用户以为自己引用了，实际没发出去）。
 *
 * 这份测试**渲染真的 `<App/>`**（真 `useChatApplication`、真 `ChatWorkSurface`、真
 * `ChatComposer`），只把 `chatApi` 的方法用 `vi.spyOn` 打桩（**不做 `vi.mock("./api")`
 * 的整模块替换**——那正是前几批反复栽的"改坏了也不红"盲区），然后从输入框敲 `@`、选候选、
 * 发消息，断言**最外层的 HTTP 调用**到底收到了什么。一条用例同时盖住 ①（主区）与 ③。
 *
 * 侧区（②）另有一条：必须真的把右侧工作面开起来（右侧对照），在**侧区那一个 composer**
 * 里选引用并发送。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../../App";
import { chatApi } from "./api";
import type { RuntimeV2ConversationReference } from "./apiTypes";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CONV = "conv-app-wiring-self";
const OTHER = "conv-app-wiring-other";
const LANE = "lane-app-wiring-main";
const BRANCH = "lane-app-wiring-branch";

const conversation = {
  id: CONV,
  title: "正在发送的会话",
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
  workspaceId: "ws-app-wiring",
};
const other = { ...conversation, id: OTHER, title: "另一个会话" };
const mainLane = {
  id: LANE,
  conversationId: CONV,
  kind: "main",
  status: "active",
  archived: false,
  archivedAt: null,
  displayName: null,
  summary: null,
  title: null,
  baseEntryExcerpt: null,
  baseEntryId: null,
  leafEntryId: null,
  createdFromEntryId: null,
  createdAt: "2026-09-01T00:00:00Z",
  sourceLaneId: null,
  isMain: true,
};
const branchLane = {
  ...mainLane,
  id: BRANCH,
  kind: "branch",
  title: "对照分支",
  isMain: false,
  sourceLaneId: LANE,
};

/** 只填界面真正读到的字段；其余用 `as` 声明完整形状（与面上其它 C3b 测试同法）。 */
const runtimeSnapshotFor = (conversationId: string, laneId: string | null) =>
  ({
    snapshotVersion: 1,
    conversationId,
    activeLaneId: laneId,
    mainLaneId: laneId,
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
  }) as never;

const expectedReferences: RuntimeV2ConversationReference[] = [
  {
    kind: "conversation",
    conversationId: OTHER,
    mode: "summary",
    entryIds: [],
  },
];

const provider = {
  id: "provider-app-wiring",
  name: "本地模型",
  kind: "openai_compatible",
  baseUrl: "http://127.0.0.1:9",
  defaultModel: "m1",
  timeoutSeconds: 60,
  enabled: true,
  isBuiltin: false,
  isDefault: true,
  configured: true,
  apiKeyConfigured: true,
  connectionState: "ready",
  lastCheckedAt: null,
  lastError: null,
  models: [
    {
      providerProfileId: "provider-app-wiring",
      modelId: "m1",
      displayName: "m1",
      source: "manual",
      enabled: true,
      isDefault: true,
      lastSeenAt: null,
    },
  ],
  createdAt: "2026-09-01T00:00:00Z",
  updatedAt: "2026-09-01T00:00:00Z",
};

/**
 * `chatApi` 的打桩表：**只桩 App 挂载/发送真正会走到的那些方法**，其余一律**响亮拒绝**
 * （用到即暴露，不静默通过）。方法是真模块上的方法（`vi.spyOn`），不是整模块替换。
 */
const stubs: Record<string, unknown> = {
  health: { status: "ok" },
  capabilities: { capabilities: [] },
  listWorkspaces: {
    items: [
      {
        id: "ws-app-wiring",
        name: "工作区",
        rootPath: "/tmp/ws-app-wiring",
        createdAt: "2026-09-01T00:00:00Z",
        updatedAt: "2026-09-01T00:00:00Z",
      },
    ],
    homePath: "/tmp",
  },
  listProviders: [provider],
  listConversations: [conversation, other],
  getConversation: { conversation, turns: [], files: [] },
  listRuntimeV2Lanes: {
    conversationId: CONV,
    activeLaneId: LANE,
    items: [mainLane, branchLane],
  },
  // 侧区快照是按 **laneId** 存的（`applyRuntimeSnapshot` 用 `activeLaneId` 当键），
  // 所以这里必须回一个 `activeLaneId` 与请求 lane 一致的对象，否则侧区拿到 null 快照。
  getRuntimeV2Snapshot: (conversationId: string, laneId?: string | null) =>
    runtimeSnapshotFor(conversationId, laneId ?? LANE),
  listInvocableSkills: [],
  listNotifications: [],
  listPendingProposals: [],
  getWorkspace: {
    conversationId: CONV,
    visible: false,
    artifacts: [],
    pendingProposals: [],
  },
  listArtifactProposals: [],
  listMemoryProposals: [],
  listTaskProposals: [],
  listKnowledgeProposals: [],
  previewRuntimeV2References: {
    conversationId: CONV,
    totalTokenEstimate: 4321,
    warnings: [],
    items: [{ conversationId: OTHER, title: other.title, tokenEstimate: 1234, text: "正文" }],
  },
  createRuntimeV2Message: { conversationId: CONV, laneId: LANE, runId: "run", userMessageId: "um" },
};

let spies: Map<string, ReturnType<typeof vi.spyOn>>;
let container: HTMLDivElement;
let root: Root;

const stubChatApi = () => {
  const map = new Map<string, ReturnType<typeof vi.spyOn>>();
  for (const key of Object.keys(chatApi) as string[]) {
    const spy = vi.spyOn(
      chatApi as never,
      key as never,
    ) as unknown as ReturnType<typeof vi.spyOn>;
    if (key in stubs) {
      const value = stubs[key];
      if (typeof value === "function") spy.mockImplementation(value as never);
      else spy.mockResolvedValue(value);
    } else {
      spy.mockRejectedValue(new Error(`测试未桩实现 chatApi.${key}`));
    }
    map.set(key, spy);
  }
  return map;
};

const createCalls = (): unknown[][] =>
  spies.get("createRuntimeV2Message")!.mock.calls as unknown[][];

const flush = (times = 12) =>
  act(async () => {
    for (let i = 0; i < times; i += 1) await Promise.resolve();
  });

const textareaOf = (selector = "textarea") =>
  document.querySelector<HTMLTextAreaElement>(selector)!;

/** 受控 textarea：React 拦直接赋值，必须走原型 setter 才能触发 onChange。 */
const type = (node: HTMLTextAreaElement, value: string) => {
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

const click = (node: Element | null | undefined) => {
  if (!node) throw new Error("找不到可点击节点");
  act(() => (node as HTMLElement).click());
};

const pressEnter = (node: HTMLTextAreaElement) =>
  act(() => {
    node.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        bubbles: true,
        cancelable: true,
      }),
    );
  });

const renderApp = async () => {
  await act(async () => {
    root.render(<App />);
  });
  await flush(20);
  await flush(20);
};

beforeEach(() => {
  // jsdom 没有布局：SessionRail 的 `scrollIntoView` 不存在，会被 rAF 回调打到。
  (Element.prototype as unknown as { scrollIntoView: () => void }).scrollIntoView =
    vi.fn();
  spies = stubChatApi();
  localStorage.clear();
  localStorage.setItem(
    "endless-task.active-conversation",
    JSON.stringify({ conversationId: CONV, workspaceId: "ws-app-wiring" }),
  );
  localStorage.setItem("endless-task.workspace", "ws-app-wiring");
  window.location.hash = "";
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("C3b C-1：真 App 里的 `@` 引用接线（3 段零守护的守护者）", () => {
  it("主区：选一条引用后发送 ⇒ chatApi 收到 references（盖住 App.tsx 主区转发 + hook→chatApi）", async () => {
    await renderApp();

    const main = textareaOf(".chat-surface:not(.is-side) textarea");
    type(main, "@");
    await flush();
    expect(
      Array.from(document.querySelectorAll(".composer-mention-name")).map(
        (node) => node.textContent,
      ),
    ).toEqual([other.title]);
    click(document.querySelector(".composer-mention-menu button"));
    await flush();
    // chip 出现了（引用确实是 composer 本地状态里的一条）。
    expect(
      document.querySelector(".composer-mention-title")?.textContent,
    ).toBe(other.title);

    type(main, "把上面的结论也带上");
    pressEnter(main);
    await flush(20);

    // 决定性断言：真 App 走完整条链之后，**HTTP 层**收到的第 5 个位置参数就是引用。
    // 少了 `App.tsx` 主区那一行转发、或 `useChatApplication` 少传 `references`，
    // 这里都会红（分别是 C-1 的 ① 与 ③）。
    expect(createCalls()).toHaveLength(1);
    const [calledConversationId, content, laneId, idempotencyKey, references] =
      createCalls()[0];
    expect(calledConversationId).toBe(CONV);
    expect(content).toBe("把上面的结论也带上");
    expect(laneId).toBe(LANE);
    // 幂等键仍在第 4 位（位置参数错位会在这里暴露）。
    expect(typeof idempotencyKey).toBe("string");
    expect(idempotencyKey).toBeTruthy();
    expect(references).toEqual(expectedReferences);

    // §6 的透传也要在这里钉住：`App.tsx` 若把 `chat.send` 的返回值丢掉（写成
    // `void chat.send(...)`），composer 永远收不到"成功"信号 ⇒ chip 不会被清空。
    expect(document.querySelector(".composer-mention-title")).toBeNull();
  });

  it("侧区：把右侧工作面开起来后在侧区 composer 里选引用并发送（盖住 App.tsx 侧区转发）", async () => {
    await renderApp();

    // 真界面路径开侧区：分支导航器 → 分支菜单 → 「右侧对照」。
    click(document.querySelector(".branch-navigator-trigger"));
    await flush();
    click(document.querySelector(".branch-navigator-row [aria-haspopup='menu']"));
    await flush();
    const compareItem = Array.from(
      document.querySelectorAll("[role='menu'] button"),
    ).find((node) => node.textContent?.includes("右侧对照"));
    click(compareItem);
    await flush(20);
    await flush(20);

    // P0-B 收口：这一刀要消灭的病就是"同一个控件看得见的名字 ≠ 听得见的名字"。
    // 这里在**真 App** 上把两者钉成同一个词：① 分支导航器菜单项（可见文案，上面正是
    // 按它找到的）、② 该工作面 `region` 的 `aria-label`（读屏名/11 处 e2e 的锚点）、
    // ③ 该面板关闭按钮的 `aria-label`。任何一处漂回旧词，这条都会红。
    // ℹ️ 最后那行 `not.toContain` 是 C0 已确立的"允许保留旧词"例外：旧词只以**负向**
    // 断言的形式存在于本文件（`grep` 兜底时要把它与真残留区分开）。
    expect(compareItem?.textContent).toBe("右侧对照");
    expect(
      container.querySelector("section.side-chat-panel")?.getAttribute("aria-label"),
    ).toBe(compareItem?.textContent);
    expect(
      container
        .querySelector(".side-chat-panel .side-close")
        ?.getAttribute("aria-label"),
    ).toBe("收起右侧对照");
    expect(container.innerHTML).not.toContain("分支对照");

    const side = textareaOf(".chat-surface.is-side textarea");
    type(side, "@");
    await flush();
    click(document.querySelector(".composer-mention-menu button"));
    await flush();
    expect(
      document.querySelector(".chat-surface.is-side .composer-mention-title")
        ?.textContent,
    ).toBe(other.title);

    type(side, "侧区也带上这条引用");
    pressEnter(side);
    await flush(20);

    // 少了 `App.tsx` 侧区那一行转发（= C-1 的 ②），这条会红。
    expect(createCalls()).toHaveLength(1);
    const [calledConversationId, content, laneId, , references] = createCalls()[0];
    expect(calledConversationId).toBe(CONV);
    expect(content).toBe("侧区也带上这条引用");
    // 侧区工作面在分支 lane 上 ⇒ 锚点 lane 是分支 lane（不是主线的）。
    expect(laneId).toBe(BRANCH);
    expect(references).toEqual(expectedReferences);

    // 同主区：侧区那一行转发也必须把 `sendSide` 的成败透传回侧区 composer。
    expect(
      document.querySelector(".chat-surface.is-side .composer-mention-title"),
    ).toBeNull();
  });
});
