// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { HubV2Event, TaskNotification } from "../chat/apiTypes";

/**
 * 切片 10 T4（item 3）：**接线那一半**。
 *
 * `ScheduledTasks.test.tsx` 直接调 `publishHubEvent`，只能证明"面板听到广播会重拉"；
 * 如果 `useAssistantHub` 收到 SSE 后忘了广播，面板会静默变回挂载时加载一次的旧行为，
 * 而那条用例**照样绿**。这里补上：真去挂 `useAssistantHub`（hub 流被桩住），
 * 让 SSE 事件穿过它，断言页面内订阅者收到了事件类型——删掉 `publishHubEvent` 这句
 * 就红。
 *
 * 切片 10 A3（Fix 1）追加**在线路径只广播一次**：SSE 在线时 `onEvent` 广播之后
 * 那次 `pollOnce` 会把同一条通知捞成 fresh，兜底广播必须只在断线时生效。
 */
type HubListener = (event: HubV2Event) => void;

let emit: HubListener | null = null;

/**
 * 切片 10 A3（Fix 1）：SSE **在线**时 `onEvent` 广播后紧跟一次 `pollOnce`，
 * 那次轮询会捞到后端**刚刚落库**的同一条通知。这个数组让用例把"轮询能看到什么"
 * 与"SSE 推了什么"分开摆：事件到达时通知已经在列表里（后端先落库、再发事件）。
 */
let polledNotifications: TaskNotification[] = [];

const streamHubV2Events = vi.fn(
  async (options: { onEvent: HubListener }): Promise<void> => {
    emit = options.onEvent;
    // 永不 settle：假装连接一直开着（否则 hook 的 while 循环会走重连分支）。
    await new Promise<void>(() => {});
  },
);

vi.mock("../chat/api", () => ({
  chatApi: {
    listNotifications: async () => polledNotifications,
    listPendingProposals: async () => [],
  },
  streamHubV2Events: (options: { onEvent: HubListener }) =>
    streamHubV2Events(options),
}));

const { useAssistantHub } = await import("./useAssistantHub");
const { subscribeHubEvents } = await import("./hubEvents");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const hubEvent = (
  type: string,
  eventSeq = 1,
): HubV2Event =>
  ({
    eventId: `evt_${eventSeq}`,
    eventSeq,
    type,
    conversationId: "conv_1",
    createdAt: "2026-09-14T09:00:00Z",
    data: {},
  }) as HubV2Event;

/** 终态通知的形状（后端 `notification.created` 就是为它发的）。 */
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

function Host() {
  useAssistantHub(() => {});
  return null;
}

let container: HTMLDivElement;
let root: Root;
let received: string[];
let unsubscribe: () => void;

const flush = async () => {
  await act(async () => {
    for (let index = 0; index < 6; index += 1) await Promise.resolve();
  });
};

beforeEach(async () => {
  received = [];
  polledNotifications = [];
  streamHubV2Events.mockClear();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(<Host />);
  });
  await flush();
  unsubscribe = subscribeHubEvents((type) => received.push(type));
});

afterEach(() => {
  unsubscribe?.();
  act(() => root.unmount());
  container.remove();
});

describe("hub 事件广播（item 3 的接线半边）", () => {
  it("hub 流建立后，SSE 事件类型会广播给页面内订阅者", async () => {
    expect(emit).not.toBeNull();

    await act(async () => {
      emit?.(hubEvent("notification.created", 1));
    });

    expect(received).toEqual(["notification.created"]);
  });

  it("广播发生在 hubEventTargets 过滤之前：与通知/提案无关的类型也照常广播", async () => {
    await act(async () => {
      emit?.(hubEvent("some.future.event", 1));
    });

    expect(received).toEqual(["some.future.event"]);
  });

  it("多次事件按到达顺序广播，并推进游标", async () => {
    await act(async () => {
      emit?.(hubEvent("notification.created", 1));
      emit?.(hubEvent("proposal.resolved", 2));
    });

    expect(received).toEqual(["notification.created", "proposal.resolved"]);
  });

  /**
   * 切片 10 A3（Fix 1）：SSE **在线**时同一条通知只广播一次。
   *
   * 后端 `notification_service` 先提交通知、再发 hub 事件，所以 `onEvent` 广播的
   * 那一刻通知已经在库里；`onEvent` 紧接着的 `pollOnce` 必然把它捞成 `fresh`。
   * 若兜底广播不按"SSE 是否断线"门控，订阅者会为同一条通知收到第二次广播
   * （「已安排」面板静默重拉两遍）——把 `if (disconnected)` 这层门控去掉就红。
   */
  it("SSE 在线时轮询捞到刚广播过的那条通知：只广播一次（兜底广播被门控住）", async () => {
    expect(emit).not.toBeNull();

    // 后端先落库、再发事件：事件到达时轮询已经能看到这条。
    polledNotifications = [notification("note_1")];
    await act(async () => {
      emit?.(hubEvent("notification.created", 1));
    });
    await flush();

    // 第一次来自 `onEvent`；紧随其后的 `pollOnce` 捞到同一条但**不**再广播。
    expect(received).toEqual(["notification.created"]);
  });
});
