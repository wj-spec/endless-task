// @vitest-environment jsdom
/**
 * C4 / R-C3：右栏「会话」视图的**真 App 端到端**接线守护。
 *
 * 为什么必须有这份文件：视图本身能红（见 `ConversationsView.dom.test.tsx`），但
 * **接线改坏了也不会红**——`App.tsx` 把 `laneTrees` 传成空数组、把会话列表传成
 * 别的来源、或「新建」另开一条路，视图层用例全都照样绿。所以这里渲染**真的
 * `<App/>`**（真 `useChatApplication`、真 `WorkspacePanel`、真 `ConversationsView`），
 * 只用 `vi.spyOn` 打桩 `chatApi`（**不整模块替换**），断言：
 *   ① 分支树就是 `listRuntimeV2Lanes` 那次请求的结果，且**打开视图不产生任何新的
 *      请求**——按**全部** spy 的调用**签名**（端点 + 实参）逐一比对，并把本视图
 *      真正消费的那 4 个端点在挂载期的调用钉成**内容级字面基线**（R-C3 的"零额外
 *      请求"承诺；只钉两个计数器会漏掉别的端点，见 C4 复审 I-2 / G-4 复审 Important-1）；
 *   ② 临时对话列表来自既有会话列表（`kind === "ephemeral"` 的在、普通的不在）；
 *   ③ 「新建」走的就是既有创建流程那一个 HTTP 调用（`createRuntimeV2TemporaryConversation`，
 *      断言**恰好一次**），并且临时对话仍开在**主区并排分栏**（侧区 `aria-label="临时对话"`）；
 *   ④ 行点击的两个 App 级回调（切分支 / 打开临时对话）**交出的实参**真的落到了 HTTP 层；
 *   ⑤ 点临时对话行打开的就是那一条会话；
 *   ⑥ **当前 lane 高亮落在正确的 lane 上**（I-1：`App.tsx` 组装处的 `currentLaneId`
 *      掐断过一次是"双绿"，这里把它钉成红的）。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../../../App";
import { chatApi } from "../../chat/api";
import type { Conversation, RuntimeV2Lane } from "../../chat/apiTypes";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CONV = "conv-c4-main";
const OTHER = "conv-c4-other";
const TEMP = "conv-c4-temp";
const LANE = "lane-c4-main";
const BRANCH = "lane-c4-branch";
const TEMP_LANE = "lane-c4-temp";

const conversation = (over: Partial<Conversation>): Conversation =>
  ({
    id: CONV,
    title: "正在看的会话",
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
    workspaceId: "ws-c4",
    ...over,
  }) as Conversation;

const mainConversation = conversation({});
const otherConversation = conversation({ id: OTHER, title: "另一个普通会话" });
const temporaryConversation = conversation({
  id: TEMP,
  title: "临时探索里的会话",
  kind: "ephemeral",
});

const lane = (over: Partial<RuntimeV2Lane>): RuntimeV2Lane =>
  ({
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
    ...over,
  }) as RuntimeV2Lane;

const mainLane = lane({ displayName: "当前主线" });
const branchLane = lane({
  id: BRANCH,
  kind: "persistent_branch",
  isMain: false,
  displayName: "对照分支",
  sourceLaneId: LANE,
});
const temporaryLane = lane({
  id: TEMP_LANE,
  conversationId: TEMP,
  kind: "temporary",
  displayName: "临时主线",
});

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

const provider = {
  id: "provider-c4",
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
      providerProfileId: "provider-c4",
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

/** 只桩界面真正会走到的方法；其余一律**响亮拒绝**（用到即暴露）。 */
const stubs: Record<string, unknown> = {
  health: { status: "ok" },
  capabilities: { capabilities: [] },
  listWorkspaces: {
    items: [
      {
        id: "ws-c4",
        name: "工作区",
        rootPath: "/tmp/ws-c4",
        createdAt: "2026-09-01T00:00:00Z",
        updatedAt: "2026-09-01T00:00:00Z",
      },
    ],
    homePath: "/tmp",
  },
  listProviders: [provider],
  listConversations: [mainConversation, otherConversation, temporaryConversation],
  getConversation: (conversationId: string) => ({
    conversation:
      conversationId === TEMP ? temporaryConversation : mainConversation,
    turns: [],
    files: [],
  }),
  listRuntimeV2Lanes: {
    conversationId: CONV,
    activeLaneId: LANE,
    items: [mainLane, branchLane],
  },
  getRuntimeV2Snapshot: (conversationId: string, laneId?: string | null) =>
    runtimeSnapshotFor(conversationId, laneId ?? LANE),
  listInvocableSkills: [],
  listNotifications: [],
  listPendingProposals: [],
  // 关键：工作区面板只在快照 `visible` 时才渲染。
  getWorkspace: {
    conversationId: CONV,
    visible: true,
    artifacts: [],
    pendingProposals: [],
  },
  listArtifactProposals: [],
  listMemoryProposals: [],
  listTaskProposals: [],
  listKnowledgeProposals: [],
  createRuntimeV2TemporaryConversation: {
    conversation: temporaryConversation,
    lane: temporaryLane,
  },
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

const callCount = (method: string) => spies.get(method)!.mock.calls.length;

/**
 * **全部** spied 端点的调用**签名**（端点 + 实参）快照。
 *
 * R-C3 的承诺是"打开这个视图**零额外请求**"——那是**端点无关**的承诺。只钉
 * `listRuntimeV2Lanes` / `listConversations` 两个计数器会漏掉别的端点（C4 复审
 * N10：把 effect 换成多拉一次 `getConversation` 时全绿）。所以零请求断言必须按
 * **全部** spy 比对（见用例 ①）。
 *
 * G-4（C4 复审 I-2 残余）把快照口径再从"调用**条数**"收到"调用**签名**"（端点 + 实参）：
 * `mock.calls` 本来就逐实参记录，快照因此也带着实参。
 *
 * ⚠️ **如实写清这条函数的检测力边界**（G-4 复审 Important-1）：
 * `spy.mock.calls` **只增**，所以"两个快照比对"能证明的只有"那一段时间里**没有任何
 * 一次调用**"——任何额外调用都会**同时**改条数与签名，两边检测力**等价**。
 * 用例 ① 的两个快照之间**按设计就是零调用**，于是"条数不变、只改实参"的偷换
 * **不可能**在这条差分里出现（窗口内不会有第二次调用被记下来）。
 * 窗口**之前**（App 挂载期）那些喂给本视图的真调用，改由用例 ① 里那几条
 * **内容级字面基线**守——**只钉了本视图真正消费的端点**，清单见用例 ① 的注释。
 */
const callSignatures = (): Record<string, unknown[][]> => {
  const signatures: Record<string, unknown[][]> = {};
  for (const [key, spy] of spies) {
    signatures[key] = (spy.mock.calls as unknown[][]).map((call) => [...call]);
  }
  return signatures;
};

const flush = (times = 20) =>
  act(async () => {
    for (let i = 0; i < times; i += 1) await Promise.resolve();
  });

/**
 * G-4：把**已排程的 timer** 跑干净——**覆盖上界 = `TIMER_DRAIN_WINDOW_MS`（200ms）**。
 *
 * `flush()` 只推进微任务；"用 `setTimeout(…, 0)` 排程、断言那一刻还没发出"的
 * 请求会整条逃逸（C4 复审 §3.3 实测：timer 形态的额外 `getConversation` 全绿）。
 * 这里用**真 timer** 逐片让出宏任务：先排程的回调会先于本函数新排的那些执行，
 * 所以 `200ms` 的窗口过去之后，"打开视图期间排下的一切**≤200ms** 的 timer"都已落地。
 *
 * ⚠️ **口径只到 200ms**（G-4 复审 M-2：旧版 `4 × setTimeout(…, 0)` 实际只覆盖 ~0ms，
 * 同一注入点换成 `setTimeout(…, 100)` 时 6 条用例**仍然全绿**）。**>200ms 的延迟
 * 排程仍然逃逸**——这是"真 timer + 固定窗口"的固有上界，不是"跑干净一切 timer"。
 * 逐片是**真等**（每片 50ms），片数 = 上界/片长 **+1 片余量**（避开"恰好卡在上界"
 * 的调度抖动）。
 */
const TIMER_DRAIN_WINDOW_MS = 200;
const TIMER_DRAIN_SLICE_MS = 50;

const drainTimers = async () => {
  const slices = Math.ceil(TIMER_DRAIN_WINDOW_MS / TIMER_DRAIN_SLICE_MS) + 1;
  for (let i = 0; i < slices; i += 1) {
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, TIMER_DRAIN_SLICE_MS));
    });
  }
  await flush();
};

const click = (node: Element | null | undefined) => {
  if (!node) throw new Error("找不到可点击节点");
  act(() => (node as HTMLElement).click());
};

/** 真界面路径打开「会话」视图：标签条的「+」→「会话」。 */
const openConversationsView = async () => {
  click(document.querySelector('.workspace-view-add'));
  await flush();
  const item = Array.from(
    document.querySelectorAll<HTMLElement>('.row-menu-pop [role="menuitem"]'),
  ).find((node) => node.textContent?.startsWith("会话"));
  click(item);
  await flush();
};

const panel = () => document.querySelector(".workspace-panel")!;

const panelLaneNames = () =>
  Array.from(panel().querySelectorAll(".workspace-lane-row .workspace-item-title")).map(
    (node) => node.textContent,
  );

const renderApp = async () => {
  await act(async () => {
    root.render(<App />);
  });
  await flush(20);
  await flush(20);
};

beforeEach(() => {
  (Element.prototype as unknown as { scrollIntoView: () => void }).scrollIntoView =
    vi.fn();
  spies = stubChatApi();
  localStorage.clear();
  localStorage.setItem(
    "endless-task.active-conversation",
    JSON.stringify({ conversationId: CONV, workspaceId: "ws-c4" }),
  );
  localStorage.setItem("endless-task.workspace", "ws-c4");
  window.location.hash = "";
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.querySelectorAll(".row-menu-pop").forEach((node) => node.remove());
  vi.restoreAllMocks();
});

describe("C4：真 App 里的「会话」视图接线", () => {
  it("①分支树来自 laneTrees：「打开视图」不产生任何新的请求（全部端点 × 完整签名）", async () => {
    await renderApp();
    expect(panel()).not.toBeNull();

    // 面板已经开过「产物」视图 ⇒ 此刻**每个端点**的调用签名就是基线。
    const signaturesBefore = callSignatures();
    expect(signaturesBefore["listRuntimeV2Lanes"].length).toBeGreaterThan(0);
    // 扫描面下界（防"把 spy 表删空也绿"）：`chatApi` 上被登记的方法条数必须够多。
    expect(Object.keys(signaturesBefore).length).toBeGreaterThan(50);
    // 零请求断言的两个快照之间**按设计没有调用**，所以"条数相同、实参不同"的偷换
    // 在 before/after 差分里**无处发生**（`spy.mock.calls` 只增 ⇒ 差分只看得见"多了
    // 一次调用"）。那一格只能靠**内容级字面基线**补：把「本视图**真正消费**的那几个
    // 端点」在挂载期（= 喂给它的那些真调用）的**完整签名**逐条钉死。
    //
    // 清单**刻意只含这 4 个**端点（以本视图的实际消费为准，**不是**全仓）：
    //   - `listRuntimeV2Lanes`   → `lanes`（分支树）
    //   - `getRuntimeV2Snapshot` → `currentLaneId` 回落到 `mainLaneIds` 的来源
    //   - `listConversations`    → `conversations`（「临时对话」列表的来源）
    //   - `getConversation`      → 当前会话内容
    // 其余 ~137 个端点在这一行**没有**内容级基线，只有"窗口内不许新增一次调用"
    // 那一层（差分口径）——这是**如实登记**的收口面，不是"任何端点都在这里红"。
    expect(signaturesBefore["listRuntimeV2Lanes"]).toEqual([[CONV]]);
    // `runtimeController.loadSnapshot` 挂载期那一次：会话 id 与 lane id 都必须对。
    expect(signaturesBefore["getRuntimeV2Snapshot"]).toEqual([[CONV, LANE]]);
    // 当前会话内容那一次。
    expect(signaturesBefore["getConversation"]).toEqual([[CONV]]);
    // 「临时对话」列表的来源：既有会话列表（工作区维度 + 汇总维度各一次，两个
    // refresh 周期）。`undefined` 是调用点确实没传的第二个实参，**不等于 `null`**。
    expect(signaturesBefore["listConversations"]).toEqual([
      ["active", "", null],
      ["active", undefined, "ws-c4"],
      ["active", "", "ws-c4"],
      ["active", "", null],
      ["active", undefined, "ws-c4"],
    ]);

    await openConversationsView();

    // 决定性断言 1：树上的两条 lane 就是那次 `listRuntimeV2Lanes` 的返回（走 laneTrees）。
    expect(panelLaneNames()).toEqual(["当前主线", "对照分支"]);

    // G-4：断言前先把**已排程的 timer** 跑干净——否则"延迟发出"的请求能整条逃逸。
    // 覆盖上界 = `TIMER_DRAIN_WINDOW_MS`（200ms）；更长的延迟排程仍然逃逸（见该函数注释）。
    await drainTimers();

    // 决定性断言 2（R-C3 的零成本承诺）：切到「会话」视图**没有任何一个端点被多打
    // 一次**——快照按**全部** spy 的**完整签名**（端点 + 实参）逐项比对，所以窗口内
    // 任何端点上的额外请求（含"多传一个实参"这种同条数变化）都会在这行红。
    //
    // ⚠️ 边界（G-4 复审 Important-1）：**差分看不见"条数相同、实参不同"**——
    // `mock.calls` 只增，条数与签名在差分里检测力等价；两个快照之间按设计**零调用**，
    // 所以那种偷换不可能在这行出现。它由上面那 4 条**内容级字面基线**（窗口**之前**
    // 喂给本视图的真调用）守。旧注释"或'条数相同但实参不同'的偷换，都在这里红"**不成立**。
    expect(callSignatures()).toEqual(signaturesBefore);
  });

  it("②临时对话列表来自既有会话列表：ephemeral 的在、普通的不在", async () => {
    await renderApp();
    await openConversationsView();

    const list = panel().querySelector(".workspace-temporary-list");
    expect(list?.textContent).toContain("临时探索里的会话");
    expect(list?.textContent).not.toContain("另一个普通会话");
    expect(list?.textContent).not.toContain("正在看的会话");
  });

  it("③「新建」走既有创建流程：同一个 HTTP 调用，且仍开在主区并排分栏", async () => {
    await renderApp();
    await openConversationsView();

    // 面板里没有第二份 composer —— 内嵌聊天（"解释 B"）会在这里露馅。
    expect(panel().querySelector("textarea")).toBeNull();

    const button = panel().querySelector<HTMLButtonElement>(".workspace-temporary-new");
    expect(button?.textContent).toContain("新建临时对话");
    click(button);
    await flush(40);

    // 与既有入口（左栏「从主线创建临时对话」/ 分支菜单「临时对话」）同一条路：
    // App.tsx 里同一个 `chat.createTemporaryConversation`，HTTP 层实参逐项一致。
    // ⚠️ 断言**调用恰好一次**（不是只看 `calls[0]`）：点一次「新建」创建两条会红
    // （C4 复审 M-1/N7）。
    expect(
      spies.get("createRuntimeV2TemporaryConversation")!.mock.calls as unknown[][],
    ).toEqual([[CONV, {}]]);

    // 仍开在主区**并排分栏**（侧区就是那个 `aria-label="临时对话"` 的 section），
    // 而不是在面板里内嵌一份工作面。
    expect(
      document.querySelector('.side-chat-panel[aria-label="临时对话"]'),
    ).not.toBeNull();
  });

  it("④点分支行：切的就是那一条 lane（实参落到 HTTP 层）", async () => {
    await renderApp();
    await openConversationsView();

    const before = callCount("getRuntimeV2Snapshot");
    const row = Array.from(
      panel().querySelectorAll<HTMLElement>(".workspace-lane-row"),
    ).find((node) => node.textContent?.includes("对照分支"));
    click(row);
    await flush(40);

    // `chat.switchLane(CONV, BRANCH)` 的落点：快照请求带着**那条 lane 的 id**。
    // App.tsx 里若把 `onSelectLane` 接成别的 lane / 不接，这里立刻红。
    const calls = spies.get("getRuntimeV2Snapshot")!.mock.calls as unknown[][];
    expect(calls.slice(before)).toContainEqual([CONV, BRANCH]);
  });

  it("⑤点临时对话行：打开的就是那一条会话（实参落到 HTTP 层）", async () => {
    await renderApp();
    await openConversationsView();

    const before = callCount("getConversation");
    click(panel().querySelector(".workspace-temporary-list button"));
    await flush(40);

    // `chat.openConversation(TEMP)` 的落点：拉的就是那条临时对话。
    const calls = spies.get("getConversation")!.mock.calls as unknown[][];
    expect(calls.slice(before)).toContainEqual([TEMP]);
  });

  /**
   * I-1（C4 复审，Important，与 C3b"3 段零守护"同构）：`App.tsx` 组装处的
   * `currentLaneId: chat.viewLaneIds[…］ ?? chat.mainLaneIds[…］ ?? null` **没有任何
   * 用例覆盖**——把它整行改成 `null` 后 `test:unit` 86/661 全绿 + `build` EXIT=0
   * （**双绿**）⇒ "当前 lane 高亮"这个交付面可以被静默掐断。
   *
   * 这里补**行为级**守护：走真界面路径打开「会话」视图，断言高亮**落在正确的 lane
   * 上**；再点分支行把查看车道切过去，断言高亮**跟着走**（钉住 `viewLaneIds` 那一支）。
   * 不 `vi.mock` 整模块、不读源码文本。
   */
  it("⑥当前 lane 高亮落在正确的 lane 上（App 组装处的 currentLaneId 真的传到了面板）", async () => {
    await renderApp();
    await openConversationsView();

    const currentLaneNames = () =>
      Array.from(
        panel().querySelectorAll(
          ".workspace-lane-row.is-current .workspace-item-title",
        ),
      ).map((node) => node.textContent);
    const ariaCurrentLaneNames = () =>
      Array.from(
        panel().querySelectorAll(
          '.workspace-lane-row[aria-current="true"] .workspace-item-title',
        ),
      ).map((node) => node.textContent);

    // 初始：`viewLaneIds` 为空 ⇒ 回落到 `mainLaneIds`（= `listRuntimeV2Lanes` 的
    // `activeLaneId`）⇒ 高亮必须落在主线上，且**有且只有一条**。
    expect(currentLaneNames()).toEqual(["当前主线"]);
    expect(ariaCurrentLaneNames()).toEqual(["当前主线"]);

    // 点「对照分支」：`chat.switchLane` 会写 `viewLaneIds[CONV] = BRANCH` ⇒
    // 高亮必须从主线**跟着走到那条分支**（这一支专门钉 `viewLaneIds`）。
    const branchRow = Array.from(
      panel().querySelectorAll<HTMLElement>(".workspace-lane-row"),
    ).find((node) => node.textContent?.includes("对照分支"));
    click(branchRow);
    await flush(40);

    expect(currentLaneNames()).toEqual(["对照分支"]);
    expect(ariaCurrentLaneNames()).toEqual(["对照分支"]);
  });
});
