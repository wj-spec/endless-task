// @vitest-environment jsdom
import { StrictMode, act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { chatApi } from "./api";
import type { Conversation, ConversationSnapshot, RuntimeV2Snapshot } from "./apiTypes";
import {
  ACTIVE_CONVERSATION_STORAGE_KEY,
  WORKSPACE_STORAGE_KEY,
} from "./chatApplicationSupport";
import { useChatApplication } from "./useChatApplication";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const conversation = (id: string, workspaceId: string): Conversation => ({
  id,
  workspaceId,
  title: id,
  status: "active",
  nextTurnOrdinal: 1,
  titleIsManual: true,
  createdAt: "2026-09-01T00:00:00Z",
  updatedAt: "2026-09-01T00:00:00Z",
  archivedAt: null,
  parentConversationId: null,
  forkTurnId: null,
  kind: "normal",
  providerProfileId: null,
  modelOverride: null,
  promotedAt: null,
});
const first = conversation("first-conversation", "first-workspace");
const target = conversation("linked-conversation", "linked-workspace");
const items = [first, target];
const snapshot = (value: Conversation): ConversationSnapshot => ({
  conversation: value,
  turns: [],
  files: [],
});
const runtime = (conversationId: string): RuntimeV2Snapshot => ({
  snapshotVersion: 1,
  conversationId,
  activeLaneId: null,
  mainLaneId: null,
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
  capabilities: [],
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function Host() {
  const chat = useChatApplication();
  return (
    <>
      <output data-testid="active">{chat.activeSnapshot?.conversation.title ?? ""}</output>
      <output data-testid="active-id">{chat.activeConversationId ?? ""}</output>
      <output data-testid="workspace">{chat.workspaceId ?? ""}</output>
      <button onClick={() => chat.selectWorkspace(first.workspaceId)}>Choose first workspace</button>
      <button onClick={() => void chat.newConversation(first.workspaceId!)}>New in first workspace</button>
    </>
  );
}

let container: HTMLDivElement;
let root: Root;
const flush = async () => {
  for (let index = 0; index < 6; index += 1) {
    await act(async () => { await Promise.resolve(); });
  }
};
const render = async () => {
  await act(async () => { root.render(<StrictMode><Host /></StrictMode>); });
  await flush();
};
const active = () => container.querySelector('[data-testid="active"]')?.textContent;
const workspace = () => container.querySelector('[data-testid="workspace"]')?.textContent;
const store = (value: Conversation) => {
  localStorage.setItem(ACTIVE_CONVERSATION_STORAGE_KEY, JSON.stringify({
    conversationId: value.id,
    workspaceId: value.workspaceId,
  }));
  localStorage.setItem(WORKSPACE_STORAGE_KEY, value.workspaceId!);
};

beforeEach(() => {
  localStorage.clear();
  history.replaceState(null, "", "/");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  vi.spyOn(chatApi, "health").mockResolvedValue({ status: "ok" } as never);
  vi.spyOn(chatApi, "capabilities").mockResolvedValue({ capabilities: [] } as never);
  vi.spyOn(chatApi, "listProviders").mockResolvedValue([]);
  vi.spyOn(chatApi, "listWorkspaces").mockResolvedValue({
    homePath: "/tmp",
    items: [first, target].map((item) => ({
      id: item.workspaceId!, name: item.workspaceId!, rootPath: `/tmp/${item.workspaceId}`,
      createdAt: item.createdAt, updatedAt: item.updatedAt,
    })),
  });
  vi.spyOn(chatApi, "listConversations").mockImplementation(async (status, _search, workspaceId) =>
    items.filter((item) => item.status === status && (!workspaceId || item.workspaceId === workspaceId)),
  );
  vi.spyOn(chatApi, "getConversation").mockImplementation(async (id) => {
    const value = items.find((item) => item.id === id);
    if (!value) throw new Error("conversation not found");
    return snapshot(value);
  });
  vi.spyOn(chatApi, "listRuntimeV2Lanes").mockImplementation(async (conversationId) => ({
    conversationId, activeLaneId: null, mainLaneId: null,
    runningLaneId: null, runningRunId: null, items: [],
  }));
  vi.spyOn(chatApi, "getRuntimeV2Snapshot").mockImplementation(async (id) => runtime(id));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("initial conversation routing with real React initialization", () => {
  it.each(["empty", "other", "same-hash", "blocked"])(
    "holds default selection until a slow explicit hash resolves (%s storage)",
    async (stored) => {
      if (stored === "other") store(first);
      if (stored === "same-hash") {
        store(target);
        localStorage.setItem(WORKSPACE_STORAGE_KEY, first.workspaceId!);
      }
      if (stored === "blocked") {
        vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
          throw new Error("storage unavailable");
        });
      }
      history.replaceState(null, "", `/#/conversation/${target.id}`);
      const linked = deferred<ConversationSnapshot>();
      vi.mocked(chatApi.getConversation).mockImplementation(async (id) =>
        id === target.id ? linked.promise : snapshot(first),
      );
      await render();
      // The first workspace/list responds immediately; it must not replace the route.
      const beforeResolution = active();
      linked.resolve(snapshot(target));
      await flush();
      expect(beforeResolution).toBe("");
      expect(active()).toBe(target.title);
      expect(container.querySelector('[data-testid="active-id"]')?.textContent).toBe(target.id);
      expect(workspace()).toBe(target.workspaceId);
      expect(location.hash).toBe(`#/conversation/${target.id}`);
    },
  );

  it("keeps a later workspace choice when the initial hash responds late", async () => {
    history.replaceState(null, "", `/#/conversation/${target.id}`);
    const linked = deferred<ConversationSnapshot>();
    vi.mocked(chatApi.getConversation).mockImplementation(async (id) =>
      id === target.id ? linked.promise : snapshot(first),
    );
    await render();
    act(() => container.querySelectorAll("button")[0]!.click());
    await flush();
    linked.resolve(snapshot(target));
    await flush();
    expect(active()).toBe(first.title);
    expect(workspace()).toBe(first.workspaceId);
    expect(location.hash).toBe(`#/conversation/${first.id}`);
  });

  it("keeps a new conversation and its workspace when hash resolution races creation", async () => {
    history.replaceState(null, "", `/#/conversation/${target.id}`);
    const linked = deferred<ConversationSnapshot>();
    const creation = deferred<Conversation>();
    const created = conversation("new-conversation", first.workspaceId!);
    vi.spyOn(chatApi, "createConversation").mockReturnValue(creation.promise);
    vi.mocked(chatApi.getConversation).mockImplementation(async (id) => {
      if (id === target.id) return linked.promise;
      return snapshot(id === created.id ? created : first);
    });
    await render();
    act(() => container.querySelectorAll("button")[1]!.click());
    await flush();
    linked.resolve(snapshot(target));
    await flush();
    creation.resolve(created);
    await flush();
    expect(active()).toBe(created.title);
    expect(workspace()).toBe(first.workspaceId);
    expect(location.hash).toBe(`#/conversation/${created.id}`);
  });

  it("opens an archived hash target rather than the active default", async () => {
    const archived: Conversation = { ...target, status: "archived" };
    history.replaceState(null, "", `/#/conversation/${target.id}`);
    vi.mocked(chatApi.getConversation).mockResolvedValue(snapshot(archived));
    vi.mocked(chatApi.listConversations).mockImplementation(async (status, _search, workspaceId) =>
      status === "archived" && workspaceId === target.workspaceId ? [archived] : [first],
    );
    await render();
    expect(active()).toBe(target.title);
    expect(workspace()).toBe(target.workspaceId);
    expect(location.hash).toBe(`#/conversation/${target.id}`);
  });

  it("restores the saved conversation in its workspace without a hash", async () => {
    store(target);
    await render();
    expect(active()).toBe(target.title);
    expect(workspace()).toBe(target.workspaceId);
  });

  it.each([true, false])("falls back after an invalid hash (saved target: %s)", async (saved) => {
    if (saved) store(target);
    history.replaceState(null, "", "/#/conversation/deleted-conversation");
    await render();
    const expected = saved ? target : first;
    expect(active()).toBe(expected.title);
    expect(workspace()).toBe(expected.workspaceId);
    expect(location.hash).toBe(`#/conversation/${expected.id}`);
  });
});
