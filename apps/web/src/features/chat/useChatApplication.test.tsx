// @vitest-environment jsdom
/**
 * `useChatApplication` 的**对外契约快照**：拆这个 2 179 行 hook 时的安全网。
 *
 * 为什么会有这条测试：`App.tsx` 依赖这个 hook 返回的 82 个字段/回调（`chat.draft`、
 * `chat.send`…），而 TypeScript 只在**用到的字段**上做检查——把领域拆成子 hook 再组装
 * 的时候，"少返回一个字段"这种事故编译期发现不了（`App.tsx` 若没用到就无声通过），
 * 用户侧要等到某个按钮点了没反应才暴露。
 *
 * 快照冻结三件事：
 * 1. 返回对象的**键集合与顺序**（少一个/多一个/挪位置都红）；
 * 2. 每个键的**类型形态**（`function` / `boolean` / `string` / `object` / `null`…），
 *    带 `|` 的声明按"并集之一"判定；
 * 3. hook 能在真实 React 下挂载（假 api 驱动一次列表加载 + 渲染 + 卸载），
 *    确保没有"只在挂载时炸"的问题。
 *
 * 有意增删字段时：改 `CONTRACT` 并在提交里说明（同后端的方法面快照做法）。
 */
import { act, useEffect } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const conversationsPayload = [
  {
    id: "conv_active",
    title: "进行中的会话",
    status: "active",
    workspaceId: "ws_1",
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-02T00:00:00Z",
  },
  {
    id: "conv_archived",
    title: "已归档的会话",
    status: "archived",
    workspaceId: "ws_1",
    createdAt: "2026-01-01T00:00:00Z",
    updatedAt: "2026-01-02T00:00:00Z",
  },
];

const workspacesPayload = [{ id: "ws_1", name: "工作区一", rootPath: "/tmp/ws1" }];

/** 只实现 hook 挂载时会用到的几个接口，其余一律拒绝（用到即暴露，不静默通过）。 */
const apiStub = {
  // 状态过滤是**服务端**做的（`listConversations(status, …)`），所以桩要按参数过滤，
  // 否则测不出"按 active 过滤"这件事——第一版桩返回了全量，断言直接红。
  listConversations: vi.fn(async (status?: string) =>
    status && status !== "all"
      ? conversationsPayload.filter((item) => item.status === status)
      : conversationsPayload,
  ),
  // 注意返回形状：`listWorkspaces()` 解析出的是 `{ items, homePath }`
  // （第一版桩写成了裸数组，hook 里的 `items.find` 当场炸出 undefined）。
  listWorkspaces: vi.fn(async () => ({
    items: workspacesPayload,
    homePath: "/tmp/home",
  })),
  health: vi.fn(async () => ({ status: "ok" })),
  listProviders: vi.fn(async () => []),
  capabilities: vi.fn(async () => ({ capabilities: [] })),
  getConversation: vi.fn(async () => null),
};

vi.mock("./api", () => {
  class ApiClientError extends Error {
    code = "stub";
    status = 0;
  }
  return {
    ApiClientError,
    chatApi: new Proxy(
      {},
      {
        get: (_target, prop: string) => {
          const existing = (apiStub as Record<string, unknown>)[prop];
          if (existing) return existing;
          return () => Promise.reject(new Error(`测试未桩实现 chatApi.${prop}`));
        },
      },
    ),
  };
});

const { useChatApplication } = await import("./useChatApplication");

type HookValue = ReturnType<typeof useChatApplication>;

/**
 * 契约快照：键 → 类型形态（`function` 与 `typeof` 的结果；`null` 单独标记）。
 * 顺序与 `useChatApplication` 的 return 字面量一致，便于人工比对。
 */
const CONTRACT: [string, string][] = [
  ["capabilities", "object|null"],
  ["activeConversationId", "string|null"],
  ["activeSnapshot", "object|null"],
  ["activeRuntimeConnection", "string|null"],
  ["activeRuntimeEvents", "object"],
  ["activeRuntimeSnapshot", "object|null"],
  ["createWorkspace", "function"],
  ["currentWorkspace", "object|null"],
  ["deleteWorkspace", "function"],
  ["homePath", "string"],
  ["refreshWorkspaces", "function"],
  ["selectWorkspace", "function"],
  ["workspaceCanCreate", "boolean"],
  ["workspaceId", "string|null"],
  ["workspaces", "object"],
  ["changeConversationStatus", "function"],
  ["closeSideConversation", "function"],
  ["conversations", "object"],
  ["conversationsRevision", "number"],
  ["createTemporaryConversation", "function"],
  ["focusTemporaryConversation", "function"],
  ["openLaneInSide", "function"],
  ["openSideConversation", "function"],
  ["promoteConversation", "function"],
  ["sendSide", "function"],
  ["setSideDraft", "function"],
  ["sideConversationId", "string|null"],
  ["sideMode", "string|null"],
  ["sideDraft", "string"],
  ["sideIsGenerating", "boolean"],
  ["sideLoading", "boolean"],
  ["sideSnapshot", "object|null"],
  ["sideRuntimeConnection", "string|null"],
  ["sideRuntimeSnapshot", "object|null"],
  ["cancel", "function"],
  ["cancelRuntimeRun", "function"],
  ["steerRuntimeRun", "function"],
  ["deleteConversation", "function"],
  ["draft", "string"],
  ["error", "string|null"],
  ["sideError", "string|null"],
  ["health", "object|null"],
  ["isGenerating", "boolean"],
  ["activeRunRunning", "boolean"],
  ["liveTurns", "object"],
  ["loading", "boolean"],
  ["newConversation", "function"],
  ["openConversation", "function"],
  ["focusTarget", "object|null"],
  ["editedUserMessages", "object"],
  ["pendingAction", "string|null"],
  ["sidePendingAction", "string|null"],
  ["providers", "object"],
  ["regenerate", "function"],
  ["resend", "function"],
  ["removeFile", "function"],
  ["resolveApproval", "function"],
  ["resolveRuntimeRecovery", "function"],
  ["renameConversation", "function"],
  ["retry", "function"],
  ["refreshCapabilities", "function"],
  ["refreshProviders", "function"],
  ["search", "string"],
  ["selectVariant", "function"],
  ["changeConversationModel", "function"],
  ["send", "function"],
  ["setDraft", "function"],
  ["dismissPrimaryError", "function"],
  ["dismissSideError", "function"],
  ["setSearch", "function"],
  ["setStatusFilter", "function"],
  ["statusFilter", "string"],
  ["laneTrees", "object"],
  ["mainLaneIds", "object"],
  ["viewLaneIds", "object"],
  ["forkLane", "function"],
  ["switchLane", "function"],
  ["promoteLane", "function"],
  ["renameLane", "function"],
  ["setLaneArchived", "function"],
  ["setArchivedLanesVisible", "function"],
  ["uploadFile", "function"],
];

let container: HTMLDivElement;
let root: Root;
let latest: HookValue | null = null;

function Host() {
  const chat = useChatApplication();
  useEffect(() => {
    latest = chat;
  });
  return <div data-testid="host" />;
}

const shapeOf = (value: unknown): string => {
  if (value === null) return "null";
  if (Array.isArray(value)) return "object";
  return typeof value;
};

const render = async () => {
  await act(async () => {
    root.render(<Host />);
  });
  // 让挂载期的 list/health/providers/capabilities 请求结算
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

beforeEach(() => {
  latest = null;
  globalThis.localStorage?.clear();
  globalThis.location.hash = "";
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  for (const fn of Object.values(apiStub)) fn.mockClear();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("useChatApplication 对外契约", () => {
  it("返回的键与顺序与快照一致，且取值的类型形态落在声明范围内", async () => {
    await render();
    expect(latest).not.toBeNull();
    const entries = Object.entries(latest as HookValue);
    expect(entries.map(([key]) => key)).toEqual(CONTRACT.map(([key]) => key));

    const wrongShape = entries
      .map(([key, value]) => {
        const declared = CONTRACT.find(([name]) => name === key)?.[1] ?? "?";
        const actual = shapeOf(value);
        const allowed = declared.split("|");
        return allowed.includes(actual) ? null : { key, declared, actual };
      })
      .filter((item): item is { key: string; declared: string; actual: string } => item !== null);
    expect(wrongShape).toEqual([]);
  });

  it("挂载时加载会话与工作区，并按 active 过滤可见会话", async () => {
    await render();
    expect(apiStub.listConversations).toHaveBeenCalled();
    expect(apiStub.listWorkspaces).toHaveBeenCalled();
    const chat = latest as HookValue;
    expect(chat.loading).toBe(false);
    expect(chat.conversations.map((item) => item.id)).toEqual(["conv_active"]);
    expect(chat.workspaces.map((item) => item.id)).toEqual(["ws_1"]);
    expect(chat.statusFilter).toBe("active");
  });

  it("所有声明为回调的字段都是真函数（拆 hook 时最容易漏掉的一类）", async () => {
    await render();
    const chat = latest as HookValue;
    const notFunctions = CONTRACT.filter(
      ([key]) => typeof (chat as unknown as Record<string, unknown>)[key] !== "function",
    )
      .filter(([, shape]) => shape === "function")
      .map(([key]) => key);
    expect(notFunctions).toEqual([]);
  });
});
