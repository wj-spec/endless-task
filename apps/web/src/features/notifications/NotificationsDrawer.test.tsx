// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const listNotifications = vi.fn(async () => [
  {
    id: "note_1",
    kind: "run_failed" as const,
    taskId: "task_1",
    runId: "taskrun_1",
    turnId: "run_abc",
    conversationId: "conv_1",
    title: "《每日简报》· 定时",
    body: "执行失败。",
    createdAt: "2026-09-10T00:00:00.000Z",
    readAt: "2026-09-10T00:01:00.000Z",
  },
  {
    id: "note_2",
    kind: "run_completed" as const,
    taskId: "task_2",
    runId: "taskrun_2",
    turnId: null,
    conversationId: "conv_2",
    title: "旧通知",
    body: "没有 turnId 的历史数据。",
    createdAt: "2026-09-09T00:00:00.000Z",
    readAt: "2026-09-09T00:01:00.000Z",
  },
]);

vi.mock("../chat/api", () => ({
  chatApi: {
    listNotifications: () => listNotifications(),
    markNotificationRead: vi.fn(async () => undefined),
    markAllNotificationsRead: vi.fn(async () => undefined),
  },
}));

const { NotificationsContent } = await import("./NotificationsDrawer");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const flush = async () => {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
};

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("通知抽屉跳转定位", () => {
  it("点击通知带上 turnId（缺省回落 runId）", async () => {
    const opened: Array<[string, string | null | undefined]> = [];
    await act(async () => {
      root.render(
        <NotificationsContent
          onOpenConversation={(conversationId, focusTurnId) =>
            opened.push([conversationId, focusTurnId])
          }
          pendingProposals={[]}
        />,
      );
    });
    await flush();

    const buttons = Array.from(
      container.querySelectorAll<HTMLButtonElement>(".memory-actions button"),
    );
    expect(buttons.length).toBe(2);
    await act(async () => buttons[0].click());
    await flush();
    await act(async () => buttons[1].click());
    await flush();

    expect(opened).toEqual([
      ["conv_1", "run_abc"],
      ["conv_2", "taskrun_2"],
    ]);
  });
});
