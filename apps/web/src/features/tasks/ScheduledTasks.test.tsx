// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CreateReminderInput,
  CreateTaskInput,
  Reminder,
  RuntimeV2Snapshot,
  TaskRun,
  TaskSummary,
  UpdateTaskInput,
} from "../chat/apiTypes";

/**
 * 切片 10 T4（item 5）：`features/tasks/` 此前**零单测**。这里钉住三件事：
 *
 * ① item 3：hub 事件到达后「已安排」面板真的重拉（新增前只在挂载时 load 一次，
 *    删掉订阅这条就红）；
 * ② item 4：`TaskRun.retryable` 变成可点的"重试"（走既有 `POST /tasks/{id}/run`），
 *    不可重试/没失败过/在飞时都不提供，**提醒** run 一律不提供（没有重跑端点）；
 * ③ 订阅成对取消、后台刷新失败不把列表换成错误态。
 */
class MockApiClientError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(
    response: Response,
    payload?: { error?: { code?: string; message?: string } },
  ) {
    super(payload?.error?.message ?? `请求失败（${response.status}）`);
    this.name = "ApiClientError";
    this.status = response.status;
    // 切片 10 T6：允许用例驱动**指名**的后端错误码（`missing_title` /
    // `missing_schedule` / `validation_error` …）。缺省仍是 `invalid_state`，
    // 既有 7 条重试用例只用 status，不受影响。
    this.code = payload?.error?.code ?? "invalid_state";
  }
}

const task = (overrides: Partial<TaskSummary> = {}): TaskSummary => ({
  id: "task_1",
  title: "每周发布检查",
  commitment: "检查发布状态",
  schedule: { kind: "weekly", weekday: 1, time: "09:00" },
  scheduleDescription: "每周一 09:00",
  status: "active",
  sourceConversationId: "conv_1",
  sourceTurnId: "turn_1",
  sourceProposalId: null,
  createdAt: "2026-09-01T00:00:00Z",
  updatedAt: "2026-09-01T00:00:00Z",
  cancelledAt: null,
  ...overrides,
});

const reminder = (overrides: Partial<Reminder> = {}): Reminder => ({
  id: "rem_1",
  title: "给妈妈打电话",
  commitment: "打个电话",
  dueAt: "2026-09-20T10:00:00Z",
  status: "pending",
  sourceConversationId: "conv_1",
  sourceTurnId: "turn_1",
  createdAt: "2026-09-01T00:00:00Z",
  firedAt: null,
  cancelledAt: null,
  ...overrides,
});

const run = (overrides: Partial<TaskRun> = {}): TaskRun => ({
  id: "taskrun_1",
  taskId: "task_1",
  trigger: "scheduled",
  status: "failed",
  conversationId: "conv_1",
  turnId: "run_v2_1",
  error: "执行失败。",
  startedAt: "2026-09-14T09:00:00Z",
  finishedAt: "2026-09-14T09:01:00Z",
  awaitingUser: false,
  awaitingNote: null,
  attempt: 1,
  retryable: true,
  ...overrides,
});

const listTasks = vi.fn(async (): Promise<TaskSummary[]> => [task()]);
const listReminders = vi.fn(async (): Promise<Reminder[]> => []);
const listTaskRuns = vi.fn(async (_taskId: string): Promise<TaskRun[]> => [run()]);
const runTask = vi.fn(async (taskId: string) => ({
  run: run({ taskId, status: "running" as const, finishedAt: null }),
}));
const pauseTask = vi.fn(async (_taskId: string) => ({
  task: task({ status: "paused" as const }),
}));
const resumeTask = vi.fn(async (_taskId: string) => ({ task: task() }));
const cancelTask = vi.fn(async (_taskId: string) => ({
  task: task({ status: "cancelled" as const }),
}));
const cancelReminder = vi.fn(async (_reminderId: string) => ({
  reminder: reminder({ status: "cancelled" as const }),
}));
// 切片 10 T6：`POST /tasks`。缺省成功（201 `{"task": …}` 被 api 层解成 task）。
const createTask = vi.fn(async (input: CreateTaskInput) =>
  task({ id: "task_created", title: input.title, commitment: input.commitment }),
);
// 批次 D Task 1：`PATCH /tasks/{id}` 与 `POST /reminders`（两个新的写入型方法）。
const updateTask = vi.fn(async (_taskId: string, input: UpdateTaskInput) =>
  task({
    title: input.title ?? "每周发布检查",
    commitment: input.commitment ?? "检查发布状态",
  }),
);
const createReminder = vi.fn(async (input: CreateReminderInput) =>
  reminder({
    id: "rem_created",
    title: input.title,
    commitment: input.commitment,
  }),
);

vi.mock("../chat/api", () => ({
  ApiClientError: MockApiClientError,
  chatApi: {
    listTasks: () => listTasks(),
    listReminders: () => listReminders(),
    listTaskRuns: (taskId: string) => listTaskRuns(taskId),
    runTask: (taskId: string) => runTask(taskId),
    pauseTask: (taskId: string) => pauseTask(taskId),
    resumeTask: (taskId: string) => resumeTask(taskId),
    cancelTask: (taskId: string) => cancelTask(taskId),
    cancelReminder: (reminderId: string) => cancelReminder(reminderId),
    createTask: (input: CreateTaskInput) => createTask(input),
    updateTask: (taskId: string, input: UpdateTaskInput) =>
      updateTask(taskId, input),
    createReminder: (input: CreateReminderInput) => createReminder(input),
  },
}));

const { ApiClientError } = await import("../chat/api");
const { hubEventSubscriberCount, publishHubEvent } = await import(
  "../hub/hubEvents"
);
const { ScheduledTasksContent } = await import("./ScheduledTasks");
const { resolveTaskSource } = await import("./taskSource");
type TaskSource = import("./taskSource").TaskSource;

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

/** 面板的 `onOpenConversation` 探针（切片 10 T4 及批次 B Task 1 的"直达"断言用）。 */
const openConversation = vi.fn((_conversationId: string) => {});

const flush = async () => {
  await act(async () => {
    for (let index = 0; index < 6; index += 1) await Promise.resolve();
  });
};

const render = async (taskSource: TaskSource | null = null) => {
  await act(async () => {
    root.render(
      <ScheduledTasksContent
        onOpenConversation={(conversationId) => openConversation(conversationId)}
        taskSource={taskSource}
      />,
    );
  });
  await flush();
};

/** 广播一个 hub 事件类型（就是 `useAssistantHub` 收到 SSE 后做的事）。 */
const emitHubEvent = async (type: string) => {
  await act(async () => {
    publishHubEvent(type);
  });
  await flush();
};

/** 某一行的动作按钮（排除 RowMenu 的「更多操作」触发按钮）。 */
const actionButtons = (title: string): HTMLButtonElement[] => {
  const row = Array.from(
    container.querySelectorAll<HTMLDivElement>(".memory-item"),
  ).find((item) =>
    (item.querySelector(".memory-content")?.textContent ?? "").includes(title),
  );
  if (!row) return [];
  return Array.from(
    row.querySelectorAll<HTMLButtonElement>(".memory-actions button"),
  ).filter((button) => button.getAttribute("aria-label") !== "更多操作");
};

const actionTexts = (title: string): string[] =>
  actionButtons(title).map((button) => button.textContent ?? "");

const buttonByText = (title: string, text: string) =>
  actionButtons(title).find((button) => button.textContent === text) ?? null;

/* ---------------------------------------------------------------- 切片 10 T6：新建表单的测试夹具 */

const createForm = (): HTMLFormElement => {
  const form = container.querySelector<HTMLFormElement>(".task-create-form");
  if (!form) throw new Error("新建表单没有渲染出来（先点「＋ 新建定时任务」）");
  return form;
};

/** 按 `<label>` 文案找到它 `for` 的那个控件（id 是 `useId()` 生成的，不能硬编码）。 */
const controlFor = (
  labelText: string,
): HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement => {
  const label = Array.from(createForm().querySelectorAll("label")).find(
    (item) => (item.textContent ?? "").trim() === labelText,
  );
  const id = label?.getAttribute("for");
  const control = id ? document.getElementById(id) : null;
  if (!control) throw new Error(`找不到「${labelText}」对应的控件`);
  return control as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
};

/** 走原生 setter + input/change 事件，React 才认这次改动。 */
const setControl = async (
  labelText: string,
  value: string,
): Promise<void> => {
  const control = controlFor(labelText);
  await act(async () => {
    const prototype =
      control instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : control instanceof HTMLSelectElement
          ? HTMLSelectElement.prototype
          : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    setter?.call(control, value);
    control.dispatchEvent(new Event("input", { bubbles: true }));
    control.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await flush();
};

const openCreateForm = async (): Promise<void> => {
  await act(async () =>
    container.querySelector<HTMLButtonElement>(".task-create-toggle")!.click(),
  );
  await flush();
};

const submitCreateForm = async (): Promise<void> => {
  await act(async () =>
    createForm().querySelector<HTMLButtonElement>('button[type="submit"]')!.click(),
  );
  await flush();
};

const createFormError = (): string | null =>
  container.querySelector(".task-create-form .task-create-error")?.textContent ??
  null;

const submitButton = (): HTMLButtonElement =>
  createForm().querySelector<HTMLButtonElement>('button[type="submit"]')!;

beforeEach(() => {
  listTasks.mockReset();
  listTasks.mockImplementation(async () => [task()]);
  listReminders.mockReset();
  listReminders.mockImplementation(async () => []);
  listTaskRuns.mockReset();
  listTaskRuns.mockImplementation(async () => [run()]);
  runTask.mockReset();
  runTask.mockImplementation(async (taskId: string) => ({
    run: run({ taskId, status: "running" as const, finishedAt: null }),
  }));
  createTask.mockReset();
  createTask.mockImplementation(async (input: CreateTaskInput) =>
    task({
      id: "task_created",
      title: input.title,
      commitment: input.commitment,
    }),
  );
  updateTask.mockReset();
  updateTask.mockImplementation(async (_taskId: string, input: UpdateTaskInput) =>
    task({
      title: input.title ?? "每周发布检查",
      commitment: input.commitment ?? "检查发布状态",
    }),
  );
  createReminder.mockReset();
  createReminder.mockImplementation(async (input: CreateReminderInput) =>
    reminder({
      id: "rem_created",
      title: input.title,
      commitment: input.commitment,
    }),
  );
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  openConversation.mockReset();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  // `RowMenu` portal 到 `document.body`，卸载后清掉残留节点，避免跨用例串扰。
  document
    .querySelectorAll('[role="menu"], [data-floating-layer]')
    .forEach((node) => node.remove());
});

describe("「已安排」面板：首屏与 hub 实时化（item 3）", () => {
  it("首屏加载任务、提醒与各自的最近一次 run", async () => {
    listReminders.mockImplementation(async () => [reminder()]);
    await render();

    expect(listTasks).toHaveBeenCalledTimes(1);
    expect(listReminders).toHaveBeenCalledTimes(1);
    expect(listTaskRuns.mock.calls.map((call) => call[0])).toEqual([
      "task_1",
      "rem_1",
    ]);
    const text = container.textContent ?? "";
    expect(text).toContain("每周发布检查");
    expect(text).toContain("给妈妈打电话");
  });

  it("hub 的 notification.created 到达即重拉（删掉订阅这条会红）", async () => {
    await render();
    expect(listTasks).toHaveBeenCalledTimes(1);

    // 后端到点触发的任务跑完了：状态变了，但这次加载不会自己发生。
    listTasks.mockImplementation(async () => [task({ status: "paused" })]);
    await emitHubEvent("notification.created");

    expect(listTasks).toHaveBeenCalledTimes(2);
    expect(container.textContent).toContain("已暂停");
  });

  it("无关的 hub 事件不重拉", async () => {
    await render();
    await emitHubEvent("proposal.pending");
    await emitHubEvent("notification.read_all");

    expect(listTasks).toHaveBeenCalledTimes(1);
    expect(listReminders).toHaveBeenCalledTimes(1);
  });

  it("卸载后 hub 事件不再触达面板（订阅成对取消）", async () => {
    await render();
    expect(hubEventSubscriberCount()).toBe(1);

    act(() => root.unmount());
    expect(hubEventSubscriberCount()).toBe(0);

    await emitHubEvent("notification.created");
    expect(listTasks).toHaveBeenCalledTimes(1);

    // afterEach 会再 unmount 一次：重新挂一个空 root，避免二次卸载报错。
    root = createRoot(container);
  });

  it("窗口重新获得焦点时补拉（后端 run 创建/开始不发 hub 事件这条缺口的兜底）", async () => {
    await render();
    expect(listTasks).toHaveBeenCalledTimes(1);

    await act(async () => {
      globalThis.dispatchEvent(new Event("focus"));
    });
    await flush();

    expect(listTasks).toHaveBeenCalledTimes(2);
  });

  it("后台刷新失败时保留已有列表，不把面板换成错误态（silent）", async () => {
    await render();
    listTasks.mockRejectedValueOnce(new Error("boom"));
    await emitHubEvent("notification.created");

    expect(container.textContent).toContain("每周发布检查");
    expect(container.textContent).not.toContain("没加载出来");
  });
});

describe("retryable → 可点重试（item 4，只对任务）", () => {
  it("失败且可重试：主按钮是「重试」，点了调 POST /tasks/{id}/run 并重拉该任务的 run", async () => {
    await render();

    expect(actionTexts("每周发布检查")).toEqual(["重试"]);
    const retry = buttonByText("每周发布检查", "重试")!;
    expect(retry.disabled).toBe(false);

    const runsBefore = listTaskRuns.mock.calls.length;
    await act(async () => retry.click());
    await flush();

    expect(runTask).toHaveBeenCalledTimes(1);
    expect(runTask).toHaveBeenCalledWith("task_1");
    expect(listTaskRuns.mock.calls.length).toBeGreaterThan(runsBefore);
    expect(listTaskRuns.mock.calls.at(-1)?.[0]).toBe("task_1");
  });

  it("失败但不可重试：没有重试入口（按钮仍是「立即执行一次」）", async () => {
    listTaskRuns.mockImplementation(async () => [run({ retryable: false })]);
    await render();

    expect(actionTexts("每周发布检查")).toEqual(["立即执行一次"]);
    expect(container.textContent).not.toContain("重试");
  });

  it("没有失败过的 run：不提供重试", async () => {
    listTaskRuns.mockImplementation(async () => [
      run({ status: "completed", retryable: false, finishedAt: null }),
    ]);
    await render();

    expect(actionTexts("每周发布检查")).toEqual(["立即执行一次"]);
  });

  it("已有在飞运行时按钮禁用且不提供重试（避免必然的 409）", async () => {
    listTaskRuns.mockImplementation(async () => [
      run({ status: "running", retryable: true, finishedAt: null }),
    ]);
    await render();

    const [button] = actionButtons("每周发布检查");
    expect(button.textContent).toBe("执行中…");
    expect(button.disabled).toBe(true);

    await act(async () => button.click());
    await flush();
    expect(runTask).not.toHaveBeenCalled();
  });

  it("暂停的任务即使上次失败可重试也不提供重试（端点会 409）", async () => {
    listTasks.mockImplementation(async () => [task({ status: "paused" })]);
    await render();

    expect(actionTexts("每周发布检查")).toEqual(["恢复"]);
  });

  it("重试返回 409 时如实说清不能重跑，且不谎报成功", async () => {
    await render();
    const runsBefore = listTaskRuns.mock.calls.length;
    runTask.mockRejectedValueOnce(
      new ApiClientError(new Response(null, { status: 409 }), {
        error: { message: "This task is already running." },
      }),
    );

    await act(async () =>
      buttonByText("每周发布检查", "重试")!.click(),
    );
    await flush();

    expect(container.querySelector(".proposal-error")?.textContent).toBe(
      "现在不能重跑：这个安排已有正在执行的运行，或者它已经不在进行中。",
    );
    // 失败路径不触发"重跑了一次"的假象刷新。
    expect(listTaskRuns.mock.calls.length).toBe(runsBefore);
  });

  it("提醒**没有**重试端点：即使 run.retryable 为 true 也不渲染重试按钮", async () => {
    listTasks.mockImplementation(async () => []);
    listReminders.mockImplementation(async () => [reminder()]);
    listTaskRuns.mockImplementation(async () => [
      run({
        taskId: "rem_1",
        status: "failed",
        retryable: true,
        awaitingUser: true,
        awaitingNote: "等你确认",
      }),
    ]);
    await render();

    const texts = actionTexts("给妈妈打电话");
    expect(texts).toEqual(["去处理"]);
    expect(container.textContent).not.toContain("重试");
  });
});

/* ================================================================================================
 * 切片 10 T6：在「已安排」面板里**创建定时任务**。
 *
 * 契约（后端 T5 `POST /tasks`，`schemas/tasks.py` = `extra="forbid"`）：
 *   {title, commitment, schedule:{kind:"daily"|"weekly"|"monthly", time:"HH:MM", weekday?/day?},
 *    conversationId, turnId} → 201 {"task": …}
 * 错误：缺字段 → 400 `missing_title|missing_commitment|missing_schedule|
 *   missing_source_conversation|missing_source_turn`；周期语义 → 400 `validation_error`
 *   + domain 的**指名** message。
 * ============================================================================================== */

/** 来源锚点：故意用与夹具里其它 id 都不同的值，好证明"不是硬编码"。 */
const SOURCE: TaskSource = { conversationId: "conv_alpha", turnId: "run_beta" };

const fillCreateForm = async (
  options: {
    kind?: "daily" | "weekly" | "monthly";
    weekday?: string;
    day?: string;
    time?: string;
    title?: string;
    commitment?: string;
  } = {},
): Promise<void> => {
  const {
    kind = "weekly",
    weekday = "3",
    day = "12",
    time = "07:30",
    title = "每周发布检查",
    commitment = "检查一遍发布流水线",
  } = options;
  await setControl("任务标题", title);
  await setControl("到点执行的内容", commitment);
  await setControl("执行周期", kind);
  await setControl("时间（HH:MM，24 小时制）", time);
  if (kind === "weekly") await setControl("周几", weekday);
  if (kind === "monthly") await setControl("每月几号（1–28）", day);
};

const rejectCreateWith = (status: number, code: string, message: string) => {
  createTask.mockRejectedValueOnce(
    new ApiClientError(new Response(null, { status }), {
      error: { code, message },
    }),
  );
};

describe("T6 ①：请求体逐字段正确 + 201 后刷新列表", () => {
  it("weekly：payload 恰好是契约里的 5 个键，schedule 形状正确；成功后清空表单并重拉列表", async () => {
    await render(SOURCE);
    await openCreateForm();
    await fillCreateForm({ kind: "weekly", weekday: "3", time: "07:30" });

    // 点提交前没有任何请求（表单是用户显式提交的）。
    expect(createTask).not.toHaveBeenCalled();
    const loadsBefore = listTasks.mock.calls.length;

    await submitCreateForm();

    expect(createTask).toHaveBeenCalledTimes(1);
    const payload = createTask.mock.calls[0]![0] as CreateTaskInput;
    // 逐字段（含类型：weekday 必须是 number，不是 "3"）。
    expect(payload).toEqual({
      title: "每周发布检查",
      commitment: "检查一遍发布流水线",
      schedule: { kind: "weekly", time: "07:30", weekday: 3 },
      conversationId: "conv_alpha",
      turnId: "run_beta",
    });
    // **恰好** 5 个键（后端 `extra="forbid"`：多一个就 400）。
    expect(Object.keys(payload).sort()).toEqual([
      "commitment",
      "conversationId",
      "schedule",
      "title",
      "turnId",
    ]);
    // schedule 只带该频度允许的键（weekly 不带 day，daily 两者都不带）。
    expect(Object.keys(payload.schedule).sort()).toEqual([
      "kind",
      "time",
      "weekday",
    ]);

    // 201 之后：列表真的重拉了，表单清空，并如实报出创建成功。
    expect(listTasks.mock.calls.length).toBe(loadsBefore + 1);
    expect(controlFor("任务标题").value).toBe("");
    expect(controlFor("到点执行的内容").value).toBe("");
    expect(container.textContent).toContain("已创建《每周发布检查》。");
  });

  it("daily：schedule 恰好 {kind,time}（不带 weekday/day）；monthly：恰好 {kind,time,day}", async () => {
    await render(SOURCE);
    await openCreateForm();
    await fillCreateForm({ kind: "daily" });
    await submitCreateForm();
    const daily = createTask.mock.calls[0]![0] as CreateTaskInput;
    expect(daily.schedule).toEqual({ kind: "daily", time: "07:30" });
    expect(Object.keys(daily.schedule).sort()).toEqual(["kind", "time"]);

    createTask.mockClear();
    await fillCreateForm({ kind: "monthly", day: "12" });
    await submitCreateForm();
    const monthly = createTask.mock.calls[0]![0] as CreateTaskInput;
    expect(monthly.schedule).toEqual({ kind: "monthly", time: "07:30", day: 12 });
    expect(Object.keys(monthly.schedule).sort()).toEqual([
      "day",
      "kind",
      "time",
    ]);
  });

  it("周期下拉只提供 daily/weekly/monthly（`once` 不是任务，后端会 400）", async () => {
    await render(SOURCE);
    await openCreateForm();

    const values = Array.from(
      controlFor("执行周期").querySelectorAll("option"),
    ).map((option) => option.value);
    expect(values).toEqual(["daily", "weekly", "monthly"]);
  });

  it("提交中禁用提交按钮（防重复提交）", async () => {
    await render(SOURCE);
    await openCreateForm();
    await fillCreateForm({ kind: "daily" });

    let resolveCreate: (value: TaskSummary) => void = () => {};
    createTask.mockImplementationOnce(
      () =>
        new Promise<TaskSummary>((resolve) => {
          resolveCreate = resolve;
        }),
    );
    await act(async () => submitButton().click());
    await flush();

    expect(submitButton().disabled).toBe(true);
    expect(submitButton().textContent).toBe("创建中…");

    await act(async () => {
      resolveCreate(task({ id: "task_created", title: "每天晚上复盘" }));
    });
    await flush();

    expect(createTask).toHaveBeenCalledTimes(1);
    expect(submitButton().disabled).toBe(false);
    expect(container.textContent).toContain("已创建《每天晚上复盘》。");
  });
});

describe("T6 ②：后端错误的显式映射（假响应驱动，不起后端）", () => {
  it.each([
    [
      "missing_title",
      "title 是必填项。",
      "请填写任务标题。",
    ],
    [
      "missing_schedule",
      "schedule 是必填项。",
      "请选择执行周期。",
    ],
    [
      "validation_error",
      "任务周期的 time 必须是 HH:MM（24 小时制）。",
      "这个安排不合法：任务周期的 time 必须是 HH:MM（24 小时制）。",
    ],
  ])(
    "400 %s → 可见文案，且不谎报成功（不重拉、不清空）",
    async (code, message, expected) => {
      await render(SOURCE);
      await openCreateForm();
      await fillCreateForm({ kind: "weekly" });
      const loadsBefore = listTasks.mock.calls.length;
      rejectCreateWith(400, code, message);

      await submitCreateForm();

      expect(createFormError()).toBe(expected);
      // 后端 message **不吞**：validation_error 的指名 message 原样出现在文案里。
      if (code === "validation_error") {
        expect(createFormError()).toContain(message);
      }
      expect(container.textContent).not.toContain("已创建");
      expect(listTasks.mock.calls.length).toBe(loadsBefore);
      expect(controlFor("任务标题").value).toBe("每周发布检查");
    },
  );

  it("网络/未知失败也如实说" , async () => {
    await render(SOURCE);
    await openCreateForm();
    await fillCreateForm({ kind: "daily" });
    createTask.mockRejectedValueOnce(new Error("boom"));

    await submitCreateForm();

    expect(createFormError()).toBe(
      "创建没有成功：连不上本地服务，请确认 API 已启动。",
    );
    expect(container.textContent).not.toContain("已创建");
  });
});

describe("T6 ③：前端校验在**提交前**挡下（不发出请求）", () => {
  it("空标题：不提交，字段级可见文案", async () => {
    await render(SOURCE);
    await openCreateForm();
    await fillCreateForm({ kind: "daily", title: "   " });

    await submitCreateForm();

    expect(createTask).not.toHaveBeenCalled();
    expect(createFormError()).toBe("请填写任务标题。");
  });

  it("非法 time（25:00）：不提交，字段级可见文案", async () => {
    await render(SOURCE);
    await openCreateForm();
    await fillCreateForm({ kind: "daily", time: "25:00" });

    await submitCreateForm();

    expect(createTask).not.toHaveBeenCalled();
    expect(container.querySelector(".task-create-form")?.textContent).toContain(
      "时间要写成 HH:MM（24 小时制），例如 09:00。",
    );
  });

  it("weekly 没选周几：不提交，字段级可见文案", async () => {
    await render(SOURCE);
    await openCreateForm();
    await fillCreateForm({ kind: "weekly", weekday: "" });

    await submitCreateForm();

    expect(createTask).not.toHaveBeenCalled();
    expect(container.querySelector(".task-create-form")?.textContent).toContain(
      "请选择周几（周一至周日）。",
    );
  });

  it("monthly 没填几号（以及越界 29）：不提交，字段级可见文案", async () => {
    await render(SOURCE);
    await openCreateForm();
    await fillCreateForm({ kind: "monthly", day: "" });
    await submitCreateForm();
    expect(createTask).not.toHaveBeenCalled();
    expect(container.querySelector(".task-create-form")?.textContent).toContain(
      "请选择每月几号（1–28）。",
    );

    await setControl("每月几号（1–28）", "29");
    await submitCreateForm();
    expect(createTask).not.toHaveBeenCalled();
    expect(container.querySelector(".task-create-form")?.textContent).toContain(
      "请选择每月几号（1–28）。",
    );
  });

  it("校验失败后改正：请求照常发出（校验不是死路）", async () => {
    await render(SOURCE);
    await openCreateForm();
    await fillCreateForm({ kind: "daily", title: "" });
    await submitCreateForm();
    expect(createTask).not.toHaveBeenCalled();

    await setControl("任务标题", "每天站会纪要");
    await submitCreateForm();

    expect(createTask).toHaveBeenCalledTimes(1);
    expect(
      (createTask.mock.calls[0]![0] as CreateTaskInput).title,
    ).toBe("每天站会纪要");
  });
});

describe("T6 ④：来源锚点必须是**当前会话/当前轮次**（拿不到就不建）", () => {
  it("conversationId/turnId 来自传下来的来源（取值断言，不是硬编码）", async () => {
    await render({ conversationId: "conv_source_x", turnId: "run_source_y" });
    await openCreateForm();
    await fillCreateForm({ kind: "daily" });
    await submitCreateForm();

    const payload = createTask.mock.calls[0]![0] as CreateTaskInput;
    expect(payload.conversationId).toBe("conv_source_x");
    expect(payload.turnId).toBe("run_source_y");
  });

  it("来源由**运行时快照**推导：`activeRunId`（当前车道当前轮）才是 turnId", async () => {
    const snapshot = {
      conversationId: "conv_live",
      activeLaneId: "lane_main",
      activeRunId: "run_live_7",
      // 别的车道里在跑的 run：**不该**被当成当前轮次（它是会话级的）。
      runningLaneId: "lane_other",
      runningRunId: "run_other_lane",
    } as unknown as RuntimeV2Snapshot;
    const source = resolveTaskSource("conv_live", snapshot);
    expect(source).toEqual({ conversationId: "conv_live", turnId: "run_live_7" });

    await render(source);
    await openCreateForm();
    await fillCreateForm({ kind: "daily" });
    await submitCreateForm();

    const payload = createTask.mock.calls[0]![0] as CreateTaskInput;
    expect(payload.conversationId).toBe("conv_live");
    expect(payload.turnId).toBe("run_live_7");
  });

  it("拿不到当前轮次（taskSource=null）：提交禁用、不发请求、也不编 id", async () => {
    await render(null);
    await openCreateForm();
    await fillCreateForm({ kind: "daily" });

    expect(submitButton().disabled).toBe(true);
    expect(container.querySelector(".task-create-form")?.textContent).toContain(
      "拿不到当前会话的当前轮次，暂时不能新建",
    );

    await act(async () => submitButton().click());
    await flush();
    expect(createTask).not.toHaveBeenCalled();
  });
});

/* ================================================================================================
 * 切片 10 A2（item 8）：状态类操作失败的**逐类归因**。
 *
 * `changeStatus` / `cancelReminder` 此前把**所有**失败塌成一句「操作失败，请重试。」——
 * 后端两种真实原因（`_repository_error_status`）：404 `NotFoundError`（这条安排/提醒
 * 已经不存在）、409 `ConflictError`/`InvalidStateError`（状态已经变了），用户能做的
 * 下一步完全不同；网络不通也不是"重试"能保证的。这里逐类点按钮，断言各自**指名**文案。
 * ============================================================================================== */

describe("A2 item 8：状态操作的失败逐类指名（不再塌成一句话）", () => {
  const rowFor = (title: string): HTMLDivElement => {
    const found = Array.from(
      container.querySelectorAll<HTMLDivElement>(".memory-item"),
    ).find((item) =>
      (item.querySelector(".memory-content")?.textContent ?? "").includes(title),
    );
    if (!found) throw new Error(`找不到《${title}》这一行`);
    return found;
  };

  const menuItem = (label: string): HTMLButtonElement | null =>
    Array.from(
      document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'),
    ).find((item) => item.textContent === label) ?? null;

  /** 打开某一行的「更多操作」菜单（`RowMenu` 是 portal 到 `document.body` 的）。 */
  const openRowMenu = async (title: string) => {
    const trigger = rowFor(title).querySelector<HTMLButtonElement>(
      'button[aria-label="更多操作"]',
    );
    if (!trigger) throw new Error(`《${title}》这一行没有「更多操作」入口`);
    await act(async () => trigger.click());
    await flush();
  };

  const selectMenuItem = async (label: string) => {
    const item = menuItem(label);
    if (!item) throw new Error(`菜单里没有「${label}」`);
    await act(async () => item.click());
    await flush();
  };

  const dialogButton = (label: string): HTMLButtonElement | null =>
    Array.from(
      container.querySelectorAll<HTMLButtonElement>('[role="dialog"] button'),
    ).find((item) => item.textContent === label) ?? null;

  const actionError = (): string =>
    container.querySelector(".proposal-error")?.textContent ?? "";

  const rejectWith = (
    status: number,
    code: string,
    message: string,
  ): InstanceType<typeof ApiClientError> =>
    new ApiClientError(new Response(null, { status }), {
      error: { code, message },
    });

  it("409/冲突：如实说状态已经变了（删掉逐类映射就会退回「操作失败，请重试。」）", async () => {
    await render();
    pauseTask.mockRejectedValueOnce(
      rejectWith(409, "invalid_state", "Task task_1 cannot move to paused."),
    );

    await openRowMenu("每周发布检查");
    await selectMenuItem("暂停");

    expect(actionError()).toBe(
      "现在不能暂停：这条安排的状态已经变了（可能刚被取消，或者已不在进行中）。",
    );
    expect(actionError()).not.toContain("操作失败，请重试。");
  });

  it("404/不存在：如实说这条安排已经不存在（可能已在别处删除）", async () => {
    await render();
    pauseTask.mockRejectedValueOnce(
      rejectWith(404, "not_found", "Task not found: task_1"),
    );

    await openRowMenu("每周发布检查");
    await selectMenuItem("暂停");

    expect(actionError()).toBe(
      "这条安排已经不存在了（可能已在别处删除），暂停没有生效。",
    );
    expect(actionError()).not.toContain("操作失败，请重试。");
  });

  it("网络失败：如实说没提交成功、连不上本地服务（不是「请重试」就完事）", async () => {
    await render();
    pauseTask.mockRejectedValueOnce(new Error("boom"));

    await openRowMenu("每周发布检查");
    await selectMenuItem("暂停");

    expect(actionError()).toBe(
      "暂停没有提交成功：连不上本地服务，请确认 API 已启动。",
    );
    expect(actionError()).not.toContain("操作失败，请重试。");
  });

  it("其它后端错误：带上状态码与后端指名 message，不吞", async () => {
    await render();
    pauseTask.mockRejectedValueOnce(
      rejectWith(500, "internal_error", "服务内部错误。"),
    );

    await openRowMenu("每周发布检查");
    await selectMenuItem("暂停");

    expect(actionError()).toBe("暂停没有成功（HTTP 500）：服务内部错误。");
    expect(actionError()).not.toContain("操作失败，请重试。");
  });

  it("取消提醒失败走同一条逐类映射（提醒/安排各自指名）", async () => {
    listTasks.mockImplementation(async () => []);
    listReminders.mockImplementation(async () => [reminder()]);
    await render();
    cancelReminder.mockRejectedValueOnce(
      rejectWith(409, "invalid_state", "Reminder cannot be cancelled."),
    );

    await openRowMenu("给妈妈打电话");
    await selectMenuItem("取消提醒");
    const confirm = dialogButton("取消提醒");
    expect(confirm).not.toBeNull();
    await act(async () => confirm!.click());
    await flush();

    expect(actionError()).toBe(
      "现在不能取消提醒：这条提醒的状态已经变了（可能刚被取消，或者已不在进行中）。",
    );
    expect(actionError()).not.toContain("操作失败，请重试。");
  });
});

/* ==============================================================================================
 * 切片 10 A3（Fix 2）：「立即执行一次」的失败**逐类指名**
 *
 * `runNow` 此前 `catch {}` 把所有失败塌成一句「触发执行失败，请重试。」——同形的归因
 * 错位（网络不通 / 这条安排已被删 / 已经在跑或不在进行中，用户能做的下一步完全不同）。
 * 这里逐类钉住文案：任何一类退回那句单一字符串就红。
 * ============================================================================================== */

describe("A3 Fix 2：「立即执行一次」的失败逐类指名（不再塌成一句话）", () => {
  const actionError = (): string =>
    container.querySelector(".proposal-error")?.textContent ?? "";

  const rejectWith = (
    status: number,
    code: string,
    message: string,
  ): InstanceType<typeof ApiClientError> =>
    new ApiClientError(new Response(null, { status }), {
      error: { code, message },
    });

  /** 没有失败 run → 主按钮是「立即执行一次」（走 `runNow`，不是 `retryRun`）。 */
  const renderRunNow = async () => {
    listTaskRuns.mockImplementation(async () => []);
    await render();
    expect(actionTexts("每周发布检查")).toEqual(["立即执行一次"]);
  };

  const clickRunNow = async () => {
    await act(async () =>
      buttonByText("每周发布检查", "立即执行一次")!.click(),
    );
    await flush();
  };

  it("409/冲突（已有在跑的运行或不在进行中）：如实说清，退回单一字符串就红", async () => {
    runTask.mockRejectedValueOnce(
      rejectWith(409, "conflict", "This task is already running."),
    );
    await renderRunNow();
    await clickRunNow();

    expect(actionError()).toBe(
      "现在不能触发执行：这个安排已有正在执行的运行，或者它已经不在进行中。",
    );
    expect(actionError()).not.toContain("触发执行失败，请重试。");
  });

  it("网络失败：如实说没提交成功、连不上本地服务（不是「请重试」就完事）", async () => {
    runTask.mockRejectedValueOnce(new Error("boom"));
    await renderRunNow();
    await clickRunNow();

    expect(actionError()).toBe(
      "触发执行没有提交成功：连不上本地服务，请确认 API 已启动。",
    );
    expect(actionError()).not.toContain("触发执行失败，请重试。");
  });

  it("404/不存在：如实说这条安排已经不存在（可能已在别处删除）", async () => {
    runTask.mockRejectedValueOnce(
      rejectWith(404, "not_found", "Task not found: task_1"),
    );
    await renderRunNow();
    await clickRunNow();

    expect(actionError()).toBe(
      "这条安排已经不存在了（可能已在别处删除），触发执行没有生效。",
    );
    expect(actionError()).not.toContain("触发执行失败，请重试。");
  });

  it("其它后端错误：带上状态码与后端指名 message，不吞", async () => {
    runTask.mockRejectedValueOnce(
      rejectWith(500, "internal_error", "服务内部错误。"),
    );
    await renderRunNow();
    await clickRunNow();

    expect(actionError()).toBe("触发执行没有成功（HTTP 500）：服务内部错误。");
    expect(actionError()).not.toContain("触发执行失败，请重试。");
  });
});

/* ==============================================================================================
 * 批次 B Task 1（ADR-1）：任务行菜单的「查看执行记录」入口
 *
 * 到点执行不再落进源会话，而是该任务**自己的执行记录会话**。面板必须有一个直达入口
 * （否则"可见"只是后端一厢情愿）；旧数据 / 降级路径里最近一次 run 仍落在源会话时，
 * 该入口**不**渲染（两个入口指向同一个会话只会让用户以为有两份记录）。
 *
 * 两条用例两向都钉住：① 有执行会话时出现，且点它打开的是**执行会话 id**（把动作里
 * 的 `lastRun.conversationId` 换成 `task.sourceConversationId`，或删掉整个入口 → 红）；
 * ② 最近一次 run 仍落在源会话时不出现（把条件里的 `lastRun.conversationId !==
 * task.sourceConversationId` 去掉 → 红）。
 * ============================================================================================== */

describe("B1：任务行菜单的「查看执行记录」入口（到点执行的落点直达）", () => {
  const rowFor = (title: string): HTMLDivElement => {
    const found = Array.from(
      container.querySelectorAll<HTMLDivElement>(".memory-item"),
    ).find((item) =>
      (item.querySelector(".memory-content")?.textContent ?? "").includes(title),
    );
    if (!found) throw new Error(`找不到《${title}》这一行`);
    return found;
  };

  const menuLabels = (): string[] =>
    Array.from(
      document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'),
    ).map((item) => item.textContent ?? "");

  const menuItem = (label: string): HTMLButtonElement | null =>
    Array.from(
      document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'),
    ).find((item) => item.textContent === label) ?? null;

  const openRowMenu = async (title: string) => {
    const trigger = rowFor(title).querySelector<HTMLButtonElement>(
      'button[aria-label="更多操作"]',
    );
    if (!trigger) throw new Error(`《${title}》这一行没有「更多操作」入口`);
    await act(async () => trigger.click());
    await flush();
  };

  const selectMenuItem = async (label: string) => {
    const item = menuItem(label);
    if (!item) throw new Error(`菜单里没有「${label}」`);
    await act(async () => item.click());
    await flush();
  };

  it("最近一次 run 落在执行会话：出现「查看执行记录」，点它打开的是执行会话 id", async () => {
    listTaskRuns.mockImplementation(async () => [
      run({ id: "taskrun_exec", conversationId: "conv_execution" }),
    ]);
    await render();

    await openRowMenu("每周发布检查");

    // 两个入口并存且语义不同：源会话 / 执行会话
    expect(menuLabels()).toContain("查看会话");
    expect(menuLabels()).toContain("查看执行记录");

    await selectMenuItem("查看执行记录");
    expect(openConversation).toHaveBeenCalledWith("conv_execution");
    expect(openConversation).not.toHaveBeenCalledWith("conv_1");

    // 「查看会话」语义不变：仍去源会话
    openConversation.mockClear();
    await openRowMenu("每周发布检查");
    await selectMenuItem("查看会话");
    expect(openConversation).toHaveBeenCalledWith("conv_1");
  });

  it("最近一次 run 仍落在源会话（旧数据/降级）：**不**出现该入口", async () => {
    listTaskRuns.mockImplementation(async () => [
      run({ id: "taskrun_source", conversationId: "conv_1" }),
    ]);
    await render();

    await openRowMenu("每周发布检查");

    // 入口不渲染（两向都钉住：不是"总是渲染"）
    expect(menuLabels()).not.toContain("查看执行记录");
    expect(menuLabels()).toContain("查看会话");
    expect(menuItem("查看执行记录")).toBeNull();
  });

  it("还没有任何 run：**不**出现该入口（没有执行记录可看）", async () => {
    listTaskRuns.mockImplementation(async () => []);
    await render();

    await openRowMenu("每周发布检查");

    expect(menuLabels()).not.toContain("查看执行记录");
    expect(menuLabels()).toContain("查看会话");
  });
});



/* ================================================================================================
 * 批次 D Task 1：两张表单（提醒创建 + 任务编辑）。
 *
 * 两张表单都在**同一个**「已安排」面板里（不新增页签）：提醒表单用自己的词表
 * （只有 `once` + `YYYY-MM-DDTHH:MM`），任务编辑从任务行菜单的「编辑」进入。
 * 这里钉住"两向"：① 缺字段/拿不到来源 → **不发请求**、可见报错；② 只有真成功
 * 才清空/刷新/报成功；失败一律保留已填内容并逐类归因（不静默、不假装成功）。
 * ============================================================================================== */

const rejectWith = (status: number, code: string, message: string) =>
  new ApiClientError(new Response(null, { status }), {
    error: { code, message },
  });

const formByClass = (className: string, hint: string): HTMLFormElement => {
  const form = container.querySelector<HTMLFormElement>(className);
  if (!form) throw new Error(`${hint}（先打开它）`);
  return form;
};

/** 在**指定表单内部**按 `<label>` 文案找控件（两套表单的 label 重名，必须限定范围）。 */
const controlIn = (
  form: HTMLFormElement,
  labelText: string,
): HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement => {
  const label = Array.from(form.querySelectorAll("label")).find(
    (item) => (item.textContent ?? "").trim() === labelText,
  );
  const id = label?.getAttribute("for");
  const control = id ? document.getElementById(id) : null;
  if (!control) throw new Error(`「${labelText}」没有对应的控件`);
  return control as HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;
};

const setIn = async (
  form: HTMLFormElement,
  labelText: string,
  value: string,
): Promise<void> => {
  const control = controlIn(form, labelText);
  await act(async () => {
    const prototype =
      control instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : control instanceof HTMLSelectElement
          ? HTMLSelectElement.prototype
          : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
    setter?.call(control, value);
    control.dispatchEvent(new Event("input", { bubbles: true }));
    control.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await flush();
};

const submitIn = async (form: HTMLFormElement): Promise<void> => {
  await act(async () =>
    form.querySelector<HTMLButtonElement>('button[type="submit"]')!.click(),
  );
  await flush();
};

/** 按文案找面板里的折叠入口（现在有两个：「＋ 新建定时任务」与「＋ 新建提醒」）。 */
const toggleByText = (text: string): HTMLButtonElement => {
  const button = Array.from(
    container.querySelectorAll<HTMLButtonElement>(".task-create-toggle"),
  ).find((item) => (item.textContent ?? "").includes(text));
  if (!button) throw new Error(`面板里没有「${text}」入口`);
  return button;
};

const openToggle = async (text: string): Promise<void> => {
  await act(async () => toggleByText(text).click());
  await flush();
};

describe("D1 ①：「新建提醒」表单（词表是自己的，落同一个「已安排」面板）", () => {
  const reminderForm = (): HTMLFormElement =>
    formByClass(".reminder-create-form", "提醒表单没有渲染出来");
  const reminderError = (): string =>
    reminderForm().querySelector(".task-create-error")?.textContent ?? "";
  const reminderNotice = (): string =>
    reminderForm().querySelector(".task-create-notice")?.textContent ?? "";

  const fillReminder = async (options: {
    title?: string;
    commitment?: string;
    at?: string;
  } = {}): Promise<void> => {
    const {
      title = "给妈妈打电话",
      commitment = "打个电话问问家里情况",
      at = "2026-09-20T15:00",
    } = options;
    await setIn(reminderForm(), "提醒标题", title);
    await setIn(reminderForm(), "到点提醒我做什么", commitment);
    await setIn(reminderForm(), "提醒时间（YYYY-MM-DDTHH:MM，本地时间）", at);
  };

  it("表单在同一次渲染里、不新增页签：面板上出现「＋ 新建提醒」", async () => {
    await render(SOURCE);
    expect(toggleByText("新建提醒")).not.toBeNull();
    // 两个入口互不干扰：新建任务的表单仍在。
    expect(toggleByText("新建定时任务")).not.toBeNull();
    // 默认收起：不渲染表单。
    expect(container.querySelector(".reminder-create-form")).toBeNull();
  });

  it("缺字段：不发出请求，字段级可见文案", async () => {
    await render(SOURCE);
    await openToggle("新建提醒");

    await submitIn(reminderForm());

    expect(createReminder).not.toHaveBeenCalled();
    const text = reminderForm().textContent ?? "";
    expect(text).toContain("请填写提醒标题。");
    expect(text).toContain("请填写到点要提醒你的内容。");
    expect(text).toContain("请填写提醒时间。");
  });

  it("时间形状错（少了 T 分隔符）：不发出请求，字段级可见文案", async () => {
    await render(SOURCE);
    await openToggle("新建提醒");
    await fillReminder({ at: "2026-09-20 15:00" });

    await submitIn(reminderForm());

    expect(createReminder).not.toHaveBeenCalled();
    expect(reminderForm().textContent).toContain("YYYY-MM-DDTHH:MM");
  });

  it("成功：payload 恰好 5 个键、dueAt 恰好 {kind:'once',at}；清空 + 刷新 + 报成功", async () => {
    await render(SOURCE);
    await openToggle("新建提醒");
    await fillReminder({ at: "2026-09-20T15:00" });
    const loadsBefore = listTasks.mock.calls.length;

    await submitIn(reminderForm());

    expect(createReminder).toHaveBeenCalledTimes(1);
    const payload = createReminder.mock.calls[0]![0] as CreateReminderInput;
    expect(payload).toEqual({
      title: "给妈妈打电话",
      commitment: "打个电话问问家里情况",
      dueAt: { kind: "once", at: "2026-09-20T15:00" },
      conversationId: "conv_alpha",
      turnId: "run_beta",
    });
    expect(Object.keys(payload).sort()).toEqual([
      "commitment",
      "conversationId",
      "dueAt",
      "title",
      "turnId",
    ]);
    // 本地墙钟原样送出（时区归一在后端）。
    expect(payload.dueAt.at).toBe("2026-09-20T15:00");
    // 201 之后：列表真的重拉了、表单清空、并如实报出创建成功。
    expect(listTasks.mock.calls.length).toBe(loadsBefore + 1);
    expect(controlIn(reminderForm(), "提醒标题").value).toBe("");
    expect(controlIn(reminderForm(), "提醒时间（YYYY-MM-DDTHH:MM，本地时间）").value).toBe(
      "",
    );
    expect(reminderNotice()).toContain("已创建《给妈妈打电话》。");
  });

  it("后端指名 400（reminder_due_in_past）：可见文案、不清空、不谎报成功、不重拉", async () => {
    await render(SOURCE);
    await openToggle("新建提醒");
    await fillReminder({ at: "2020-01-01T00:00" });
    const loadsBefore = listTasks.mock.calls.length;
    createReminder.mockRejectedValueOnce(
      rejectWith(400, "reminder_due_in_past", "提醒时间已过去（…）。"),
    );

    await submitIn(reminderForm());

    expect(reminderError()).toBe(
      "提醒时间已经过去了，请改成还没到的时刻（后端按当前分钟判定）。",
    );
    expect(reminderForm().textContent).not.toContain("已创建");
    expect(listTasks.mock.calls.length).toBe(loadsBefore);
    // 已填内容保留（用户改个时间就能重试），且表单还开着。
    expect(controlIn(reminderForm(), "提醒标题").value).toBe("给妈妈打电话");
  });

  it("拿不到当前轮次（taskSource=null）：提交禁用、栏位说明、不发请求", async () => {
    await render(null);
    await openToggle("新建提醒");
    await fillReminder();

    const submit = reminderForm().querySelector<HTMLButtonElement>(
      'button[type="submit"]',
    )!;
    expect(submit.disabled).toBe(true);
    expect(reminderForm().textContent).toContain(
      "拿不到当前会话的当前轮次，暂时不能新建",
    );

    await act(async () => submit.click());
    await flush();
    expect(createReminder).not.toHaveBeenCalled();
  });
});

describe("D1 ②：任务编辑表单（入口在任务行菜单，走 PATCH）", () => {
  const rowFor = (title: string): HTMLDivElement => {
    const found = Array.from(
      container.querySelectorAll<HTMLDivElement>(".memory-item"),
    ).find((item) =>
      (item.querySelector(".memory-content")?.textContent ?? "").includes(title),
    );
    if (!found) throw new Error(`找不到《${title}》这一行`);
    return found;
  };

  const menuItem = (label: string): HTMLButtonElement | null =>
    Array.from(
      document.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'),
    ).find((item) => item.textContent === label) ?? null;

  const openEditForm = async (title: string): Promise<void> => {
    const trigger = rowFor(title).querySelector<HTMLButtonElement>(
      'button[aria-label="更多操作"]',
    );
    if (!trigger) throw new Error(`《${title}》这一行没有「更多操作」入口`);
    await act(async () => trigger.click());
    await flush();
    const edit = menuItem("编辑");
    if (!edit) throw new Error("菜单里没有「编辑」");
    await act(async () => edit.click());
    await flush();
  };

  const editForm = (): HTMLFormElement =>
    formByClass(".task-edit-form", "编辑表单没有渲染出来");
  const editError = (): string =>
    editForm().querySelector(".task-create-error")?.textContent ?? "";
  const panelNotice = (): string =>
    container.querySelector(".task-create-notice")?.textContent ?? "";

  it("预填正确：标题/内容/周期/时间/周几逐字段读回当前排期", async () => {
    await render(SOURCE);

    await openEditForm("每周发布检查");

    expect(controlIn(editForm(), "任务标题").value).toBe("每周发布检查");
    expect(controlIn(editForm(), "到点执行的内容").value).toBe("检查发布状态");
    expect(controlIn(editForm(), "执行周期").value).toBe("weekly");
    expect(controlIn(editForm(), "时间（HH:MM，24 小时制）").value).toBe("09:00");
    expect(controlIn(editForm(), "周几").value).toBe("1");
  });

  it("monthly 的预填：day 回填、不渲染周几控件", async () => {
    listTasks.mockImplementation(async () => [
      task({ schedule: { kind: "monthly", day: 12, time: "09:00" } }),
    ]);
    await render(SOURCE);

    await openEditForm("每周发布检查");

    expect(controlIn(editForm(), "执行周期").value).toBe("monthly");
    expect(controlIn(editForm(), "每月几号（1–28）").value).toBe("12");
    expect(
      Array.from(editForm().querySelectorAll("label")).some(
        (item) => (item.textContent ?? "").trim() === "周几",
      ),
    ).toBe(false);
  });

  it("成功：PATCH 只带允许面三个键；关表单 + 成功提示 + 列表刷新", async () => {
    await render(SOURCE);
    await openEditForm("每周发布检查");
    const loadsBefore = listTasks.mock.calls.length;

    await setIn(editForm(), "任务标题", "每日站会纪要");
    await submitIn(editForm());

    expect(updateTask).toHaveBeenCalledTimes(1);
    const [taskId, payload] = updateTask.mock.calls[0]!;
    expect(taskId).toBe("task_1");
    expect(payload).toEqual({
      title: "每日站会纪要",
      commitment: "检查发布状态",
      schedule: { kind: "weekly", time: "09:00", weekday: 1 },
    });
    // `status` / 来源 / created_at **不是**允许面（后端 extra="forbid"）。
    expect(Object.keys(payload).sort()).toEqual([
      "commitment",
      "schedule",
      "title",
    ]);
    // 可见收尾：成功提示 + 真的重拉了列表；表单关掉。
    expect(panelNotice()).toContain("已保存《每日站会纪要》");
    expect(panelNotice()).toContain("列表已刷新");
    expect(listTasks.mock.calls.length).toBe(loadsBefore + 1);
    expect(container.querySelector(".task-edit-form")).toBeNull();
  });

  it("失败（404）：保留用户已填内容、表单不关、如实说这条安排已不存在、不重拉", async () => {
    await render(SOURCE);
    await openEditForm("每周发布检查");
    const loadsBefore = listTasks.mock.calls.length;
    updateTask.mockRejectedValueOnce(
      rejectWith(404, "not_found", "Task not found: task_1"),
    );

    await setIn(editForm(), "任务标题", "改了一半的标题");
    await submitIn(editForm());

    expect(editError()).toBe(
      "这条安排已经不存在了（可能已在别处删除），编辑没有生效。",
    );
    // 不让用户白改：输入保留、表单还开着。
    expect(controlIn(editForm(), "任务标题").value).toBe("改了一半的标题");
    expect(panelNotice()).toBe("");
    expect(listTasks.mock.calls.length).toBe(loadsBefore);
  });

  it("失败（409 已取消 / 400 排期非法）：逐类归因，不塌成一句话", async () => {
    await render(SOURCE);
    await openEditForm("每周发布检查");

    updateTask.mockRejectedValueOnce(
      rejectWith(409, "invalid_state", "Task task_1 is cancelled and cannot be updated."),
    );
    await submitIn(editForm());
    expect(editError()).toBe("现在不能编辑：这条安排已经取消，取消是终态。");

    updateTask.mockRejectedValueOnce(
      rejectWith(
        400,
        "validation_error",
        "一次性事项不属于任务，任务只支持周期安排。",
      ),
    );
    await submitIn(editForm());
    expect(editError()).toBe(
      "这个安排不合法：一次性事项不属于任务，任务只支持周期安排。",
    );
  });

  it("失败（本地校验）：不发请求，字段级可见文案", async () => {
    await render(SOURCE);
    await openEditForm("每周发布检查");

    await setIn(editForm(), "任务标题", "   ");
    await submitIn(editForm());

    expect(updateTask).not.toHaveBeenCalled();
    expect(editForm().textContent).toContain("请填写任务标题。");
  });

  it("如实写清「改期不会立刻补跑」这条语义（不假装）", async () => {
    await render(SOURCE);

    await openEditForm("每周发布检查");

    expect(editForm().textContent).toContain("改期不会立刻补跑");
  });

  it("取消：关掉表单、不发请求", async () => {
    await render(SOURCE);
    await openEditForm("每周发布检查");

    const cancel = Array.from(
      editForm().querySelectorAll<HTMLButtonElement>("button"),
    ).find((item) => item.textContent === "取消")!;
    await act(async () => cancel.click());
    await flush();

    expect(updateTask).not.toHaveBeenCalled();
    expect(container.querySelector(".task-edit-form")).toBeNull();
  });
});
