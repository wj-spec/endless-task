// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CreateTaskInput, RuntimeV2Snapshot, TaskSummary } from "../chat/apiTypes";

/**
 * 切片 10 T6（验收 ④，接线层）：「已安排」面板拿到的**来源锚点**是
 * `AssistantPanel` 从 `conversationId` + `runtimeSnapshot` 推出来的当前会话/当前轮次，
 * 而不是面板自己编的东西。
 *
 * 证据链（本用例钉住最后一段；前三段是既有代码）：
 *   `App.tsx:726` `conversationId={chat.activeConversationId}` /
 *   `runtimeSnapshot={chat.activeRuntimeSnapshot}`
 *   → `useChatApplication.ts:607`（按 会话+当前车道 取快照）
 *   → `useRuntimeEventBridge.hydrateActiveTurn` → `runtimeController.loadSnapshot`
 *   → `AssistantPanel` 这一行 `resolveTaskSource(...)` → 面板 → `POST /tasks`
 * 快照不是当前会话的（或该会话还没有任何一轮）时 → `taskSource=null` → 提交禁用。
 */

class MockApiClientError extends Error {
  readonly status = 400;
  readonly code = "invalid_state";
}

const listTasks = vi.fn(async (): Promise<TaskSummary[]> => []);
const createTask = vi.fn(async (input: CreateTaskInput): Promise<TaskSummary> => ({
  id: "task_created",
  title: input.title,
  commitment: input.commitment,
  schedule: input.schedule,
  scheduleDescription: "每天 07:30",
  status: "active",
  sourceConversationId: input.conversationId,
  sourceTurnId: input.turnId,
  sourceProposalId: null,
  createdAt: "2026-09-14T00:00:00Z",
  updatedAt: "2026-09-14T00:00:00Z",
  cancelledAt: null,
}));

vi.mock("../chat/api", () => ({
  ApiClientError: MockApiClientError,
  chatApi: {
    listTasks: () => listTasks(),
    listReminders: async () => [],
    listTaskRuns: async () => [],
    createTask: (input: CreateTaskInput) => createTask(input),
  },
}));

const { AssistantPanel } = await import("./AssistantPanel");

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

const flush = async () => {
  await act(async () => {
    for (let index = 0; index < 6; index += 1) await Promise.resolve();
  });
};

const snapshotWith = (state: Record<string, unknown>): RuntimeV2Snapshot =>
  state as unknown as RuntimeV2Snapshot;

const renderPanel = async (snapshot: RuntimeV2Snapshot | null) => {
  await act(async () => {
    root.render(
      <AssistantPanel
        capabilities={null}
        conversationId="conv_panel"
        onCapabilitiesChanged={() => {}}
        onClose={() => {}}
        onOpenConversation={() => {}}
        onTabChange={() => {}}
        pendingProposals={[]}
        onProvidersChanged={() => {}}
        runtimeConnection={null}
        runtimeEvents={[]}
        runtimeLanes={[]}
        runtimeSnapshot={snapshot}
        tab="scheduled"
        unreadCount={0}
        workspaceId={null}
        workspaces={[]}
      />,
    );
  });
  await flush();
};

const form = () => container.querySelector<HTMLFormElement>(".task-create-form")!;

const controlFor = (labelText: string) => {
  const label = Array.from(form().querySelectorAll("label")).find(
    (item) => (item.textContent ?? "").trim() === labelText,
  );
  const id = label?.getAttribute("for");
  const control = id ? document.getElementById(id) : null;
  if (!control) throw new Error(`找不到「${labelText}」对应的控件`);
  return control as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
};

const setControl = async (labelText: string, value: string) => {
  const control = controlFor(labelText);
  await act(async () => {
    const prototype =
      control instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : control instanceof HTMLSelectElement
          ? HTMLSelectElement.prototype
          : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, "value")?.set?.call(control, value);
    control.dispatchEvent(new Event("input", { bubbles: true }));
    control.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await flush();
};

const submitButton = () =>
  form().querySelector<HTMLButtonElement>('button[type="submit"]')!;

beforeEach(() => {
  createTask.mockReset();
  createTask.mockImplementation(async (input: CreateTaskInput) => ({
    id: "task_created",
    title: input.title,
    commitment: input.commitment,
    schedule: input.schedule,
    scheduleDescription: "每天 07:30",
    status: "active",
    sourceConversationId: input.conversationId,
    sourceTurnId: input.turnId,
    sourceProposalId: null,
    createdAt: "2026-09-14T00:00:00Z",
    updatedAt: "2026-09-14T00:00:00Z",
    cancelledAt: null,
  }));
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

describe("T6 ④：面板的 conversationId/turnId 来自当前会话的运行时快照", () => {
  it("快照的 activeRunId（当前车道当前轮）就是提交时的 turnId", async () => {
    await renderPanel(
      snapshotWith({
        conversationId: "conv_panel",
        activeLaneId: "lane_main",
        activeRunId: "run_panel_current",
        runningLaneId: "lane_other",
        runningRunId: "run_other_lane",
      }),
    );
    await act(async () =>
      container.querySelector<HTMLButtonElement>(".task-create-toggle")!.click(),
    );
    await flush();
    await setControl("任务标题", "每天早上复盘");
    await setControl("到点执行的内容", "把昨天的进展写成三条");
    await setControl("执行周期", "daily");

    await act(async () => submitButton().click());
    await flush();

    expect(createTask).toHaveBeenCalledTimes(1);
    const payload = createTask.mock.calls[0]![0] as CreateTaskInput;
    expect(payload.conversationId).toBe("conv_panel");
    // 取的是当前车道的当前轮，不是别的车道里正在跑的 run。
    expect(payload.turnId).toBe("run_panel_current");
    expect(payload.schedule).toEqual({ kind: "daily", time: "09:00" });
  });

  it("快照还是别的会话的 → 拿不到当前轮次：提交禁用、不发请求", async () => {
    await renderPanel(
      snapshotWith({
        conversationId: "conv_other",
        activeLaneId: "lane_main",
        activeRunId: "run_other",
      }),
    );
    await act(async () =>
      container.querySelector<HTMLButtonElement>(".task-create-toggle")!.click(),
    );
    await flush();

    expect(submitButton().disabled).toBe(true);
    await act(async () => submitButton().click());
    await flush();
    expect(createTask).not.toHaveBeenCalled();
  });

  it("会话还没有任何一轮（activeRunId 为空）→ 提交禁用、不发请求", async () => {
    await renderPanel(
      snapshotWith({
        conversationId: "conv_panel",
        activeLaneId: "lane_main",
        activeRunId: null,
      }),
    );
    await act(async () =>
      container.querySelector<HTMLButtonElement>(".task-create-toggle")!.click(),
    );
    await flush();

    expect(submitButton().disabled).toBe(true);
    expect(createTask).not.toHaveBeenCalled();
  });
});
