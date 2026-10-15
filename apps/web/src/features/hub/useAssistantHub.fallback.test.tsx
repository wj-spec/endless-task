// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskNotification } from "../chat/apiTypes";

/**
 * 切片 10 A2（item 6）：hub **断线兜底轮询**那一半的接线。
 *
 * SSE 不可用（`streamHubV2Events` 直接抛）时 `useAssistantHub` 才启用 10s 轮询；
 * 此前 `pollOnce` 只更新自己的 unread/proposals、**从不** `publishHubEvent`——hub
 * 断线期间后端到点触发的任务/提醒**终态通知**因此推不到「已安排」面板（只能靠面板
 * 自己的 focus/visibility 补拉）。这里把 SSE 桩成失败，让轮询真的拿到一条新通知，
 * 断言页面内订阅者收到广播：把 `broadcastHubEvent("notification.created")` 从
 * `pollOnce` 里拿掉就红。
 */
const FALLBACK_POLL_MS = 10_000;

const notification = (id = "note_1"): TaskNotification => ({
  id,
  kind: "run_failed",
  taskId: "task_1",
  runId: "taskrun_1",
  turnId: "run_v2_1",
  conversationId: "conv_1",
  title: "《提醒·给妈妈打电话》",
  body: "本次提醒未执行（运行已被取消）。",
  createdAt: "2026-09-14T09:00:00Z",
  readAt: null,
});

const listNotifications = vi.fn(async (): Promise<TaskNotification[]> => []);
const listPendingProposals = vi.fn(async () => []);

vi.mock("../chat/api", () => ({
  chatApi: {
    listNotifications: () => listNotifications(),
    listPendingProposals: () => listPendingProposals(),
  },
  // SSE 不可用：`openStream` 进 catch → disconnected → 启动 10s 轮询兜底。
  streamHubV2Events: async () => {
    throw new Error("SSE 不可用");
  },
}));

const { useAssistantHub } = await import("./useAssistantHub");
const { subscribeHubEvents } = await import("./hubEvents");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const fresh: TaskNotification[][] = [];

function Host() {
  useAssistantHub((items) => {
    fresh.push(items);
  });
  return null;
}

let container: HTMLDivElement;
let root: Root;
let received: string[];
let unsubscribe: () => void;

const flush = async () => {
  await act(async () => {
    for (let index = 0; index < 8; index += 1) await Promise.resolve();
  });
};

/** 推进兜底轮询（一轮 10s）并等它的异步落定。 */
const advancePoll = async () => {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(FALLBACK_POLL_MS);
  });
  await flush();
};

/** 挂载 hook（**在配好 `listNotifications` 之后**调，才能驱动首屏那次基线 poll）。 */
const mount = async () => {
  await act(async () => {
    root.render(<Host />);
  });
  await flush();
  unsubscribe = subscribeHubEvents((type) => received.push(type));
};

beforeEach(() => {
  vi.useFakeTimers();
  received = [];
  fresh.length = 0;
  listNotifications.mockReset();
  listNotifications.mockImplementation(async () => []);
  listPendingProposals.mockReset();
  listPendingProposals.mockImplementation(async () => []);
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  unsubscribe?.();
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
});

describe("hub 断线兜底轮询的广播（item 6）", () => {
  it("SSE 不可用时，轮询拿到的终态通知会广播给页面内订阅者", async () => {
    await mount();
    // 首屏那次 poll 只是把服务端已有的列表灌成基线：不广播、也不算"新通知"。
    expect(received).toEqual([]);
    expect(fresh).toEqual([]);

    // 断线期间，后端到点触发的提醒落终态 → 通知列表里多出一条。
    listNotifications.mockImplementation(async () => [notification()]);
    await advancePoll();

    expect(received).toEqual(["notification.created"]);
    // 既有行为不变：新通知照样交给调用方（「已安排」面板的 `onFreshNotifications`）。
    expect(fresh.map((items) => items.map((item) => item.id))).toEqual([
      ["note_1"],
    ]);
  });

  it("挂载时服务端**已有**的通知不算新通知：不广播（否则每次挂载都凭空刷一轮）", async () => {
    listNotifications.mockImplementation(async () => [
      notification("note_old"),
    ]);
    await mount();
    await advancePoll();

    expect(received).toEqual([]);
    expect(fresh).toEqual([]);
  });

  it("轮询拿到的还是那一条（不是新到的）：也不重复广播", async () => {
    await mount();
    listNotifications.mockImplementation(async () => [notification()]);

    await advancePoll();
    expect(received).toEqual(["notification.created"]);

    await advancePoll();
    expect(received).toEqual(["notification.created"]);
  });
});
