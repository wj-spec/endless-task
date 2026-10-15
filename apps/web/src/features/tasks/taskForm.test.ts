import { describe, expect, it } from "vitest";
import { ApiClientError } from "../chat/api";
import type { CreateTaskInput, TaskSummary } from "../chat/apiTypes";
import type { TaskSource } from "./taskSource";
import {
  TASK_CREATE_ERROR_TEXT,
  TASK_SCHEDULE_KINDS,
  buildCreateTaskPayload,
  buildUpdateTaskPayload,
  createTaskErrorText,
  emptyTaskFormValues,
  taskFormValuesFromTask,
  updateTaskErrorText,
  validateTaskForm,
} from "./taskForm";

/**
 * 切片 10 T6：表单的**纯逻辑**三件事，逐条钉住——
 * ① 组包：**恰好**契约里那 5 个键 + schedule 形状随频度（后端 `extra="forbid"`）；
 * ② 提交前校验：空标题 / 非法 time / weekly 缺 weekday / monthly 缺 day；
 * ③ 错误映射：后端 5 个 `missing_*` 码 + `validation_error`（指名 message 原样；它是
 *    **多用途**码，只有排期域才配"这个安排不合法"前缀）+ 409/网络。
 */
const SOURCE: TaskSource = { conversationId: "conv_1", turnId: "run_1" };

const values = (overrides: Partial<ReturnType<typeof emptyTaskFormValues>> = {}) => ({
  ...emptyTaskFormValues(),
  title: "每周发布检查",
  commitment: "检查发布流水线",
  ...overrides,
});

const errorWith = (status: number, code: string, message: string) =>
  new ApiClientError({ ok: false, status } as Response, {
    error: { code, message },
  });

describe("buildCreateTaskPayload：逐字段契约（恰好 5 个键）", () => {
  it("weekly：键集合与 schedule 形状完全对齐后端 CreateTaskBody", () => {
    const payload = buildCreateTaskPayload(
      values({ kind: "weekly", time: "07:30", weekday: "3" }),
      SOURCE,
    );
    expect(payload).toEqual({
      title: "每周发布检查",
      commitment: "检查发布流水线",
      schedule: { kind: "weekly", time: "07:30", weekday: 3 },
      conversationId: "conv_1",
      turnId: "run_1",
    });
    expect(Object.keys(payload).sort()).toEqual([
      "commitment",
      "conversationId",
      "schedule",
      "title",
      "turnId",
    ]);
    // weekday 是 number（后端要求 int，且拒绝 bool）。
    expect(typeof payload.schedule.weekday).toBe("number");
  });

  it("daily：schedule 只有 kind/time（混带 weekday/day 会被后端 400）", () => {
    const payload = buildCreateTaskPayload(values({ kind: "daily" }), SOURCE);
    expect(payload.schedule).toEqual({ kind: "daily", time: "09:00" });
    expect(Object.keys(payload.schedule).sort()).toEqual(["kind", "time"]);
  });

  it("monthly：schedule 只有 kind/time/day（day 是 number）", () => {
    const payload = buildCreateTaskPayload(
      values({ kind: "monthly", day: "12" }),
      SOURCE,
    );
    expect(payload.schedule).toEqual({
      kind: "monthly",
      time: "09:00",
      day: 12,
    });
    expect(Object.keys(payload.schedule).sort()).toEqual(["day", "kind", "time"]);
  });

  it("title/commitment/time 去掉首尾空白（后端按 trim 后的长度判空/上限）", () => {
    const payload = buildCreateTaskPayload(
      values({ title: "  标题  ", commitment: "  内容  ", time: " 07:30 " }),
      SOURCE,
    );
    expect(payload.title).toBe("标题");
    expect(payload.commitment).toBe("内容");
    expect(payload.schedule.time).toBe("07:30");
  });

  it("来源锚点原样透传（不是常量、也不是本地拼的）", () => {
    const payload = buildCreateTaskPayload(values(), {
      conversationId: "conv_xyz",
      turnId: "run_abc",
    });
    expect(payload.conversationId).toBe("conv_xyz");
    expect(payload.turnId).toBe("run_abc");
  });

  it("`once` 不是任务：表单提供的周期里没有它（后端对它直接 400）", () => {
    expect([...TASK_SCHEDULE_KINDS]).toEqual(["daily", "weekly", "monthly"]);
    expect(TASK_SCHEDULE_KINDS).not.toContain("once");
  });
});

describe("validateTaskForm：提交前挡下（验收 ③ 的口径）", () => {
  it("合法输入没有错误", () => {
    expect(validateTaskForm(values({ kind: "weekly", weekday: "1" }))).toEqual({});
    expect(validateTaskForm(values({ kind: "monthly", day: "28" }))).toEqual({});
    expect(validateTaskForm(values({ kind: "daily" }))).toEqual({});
  });

  it("空标题 / 只有空白 → title 字段错误", () => {
    expect(validateTaskForm(values({ title: "" })).title).toBe("请填写任务标题。");
    expect(validateTaskForm(values({ title: "   " })).title).toBe(
      "请填写任务标题。",
    );
  });

  it("空内容 → commitment 字段错误", () => {
    expect(validateTaskForm(values({ commitment: " " })).commitment).toBe(
      "请填写到点要执行的内容。",
    );
  });

  it("非法 time → time 字段错误（与后端 `^([01]\\d|2[0-3]):[0-5]\\d$` 同口径）", () => {
    for (const time of ["", "9:00", "25:00", "24:00", "07:60", "0730", "7:3"]) {
      expect(validateTaskForm(values({ time })).time).toBe(
        "时间要写成 HH:MM（24 小时制），例如 09:00。",
      );
    }
    // 边界合法值。
    for (const time of ["00:00", "23:59", "09:05"]) {
      expect(validateTaskForm(values({ time })).time).toBeUndefined();
    }
  });

  it("weekly 缺 weekday（以及越界 0/8）→ weekday 字段错误；day 不参与校验", () => {
    expect(
      validateTaskForm(values({ kind: "weekly", weekday: "" })).weekday,
    ).toBe("请选择周几（周一至周日）。");
    expect(
      validateTaskForm(values({ kind: "weekly", weekday: "0" })).weekday,
    ).toBe("请选择周几（周一至周日）。");
    expect(
      validateTaskForm(values({ kind: "weekly", weekday: "8" })).weekday,
    ).toBe("请选择周几（周一至周日）。");
    expect(
      validateTaskForm(values({ kind: "weekly", weekday: "7", day: "" })).day,
    ).toBeUndefined();
  });

  it("monthly 缺 day（以及越界 0/29）→ day 字段错误；weekday 不参与校验", () => {
    expect(validateTaskForm(values({ kind: "monthly", day: "" })).day).toBe(
      "请选择每月几号（1–28）。",
    );
    expect(validateTaskForm(values({ kind: "monthly", day: "0" })).day).toBe(
      "请选择每月几号（1–28）。",
    );
    expect(validateTaskForm(values({ kind: "monthly", day: "29" })).day).toBe(
      "请选择每月几号（1–28）。",
    );
    expect(
      validateTaskForm(values({ kind: "monthly", day: "1", weekday: "" })).weekday,
    ).toBeUndefined();
  });
});

describe("createTaskErrorText：后端错误的可见映射表", () => {
  it("表格里的每个码都有一条固定中文文案（逐个断言，防表格漂移）", () => {
    const expected: Record<string, string> = {
      missing_title: "请填写任务标题。",
      missing_commitment: "请填写到点要执行的内容。",
      missing_schedule: "请选择执行周期。",
      missing_source_conversation: "没有可作为来源的当前会话，创建没有提交。",
      missing_source_turn: "没有可作为来源的当前轮次，创建没有提交。",
    };
    expect(TASK_CREATE_ERROR_TEXT).toEqual(expected);
    for (const [code, text] of Object.entries(expected)) {
      expect(createTaskErrorText(errorWith(400, code, "后端文案"))).toBe(text);
    }
  });

  it("validation_error（排期域）：点名 message 原样出现，并带排期前缀（不重写词表、不吞）", () => {
    const message = "任务周期的 time 必须是 HH:MM（24 小时制）。";
    expect(createTaskErrorText(errorWith(400, "validation_error", message))).toBe(
      `这个安排不合法：${message}`,
    );
    expect(
      createTaskErrorText(
        errorWith(400, "validation_error", "任务周期包含不支持的字段：timezone"),
      ),
    ).toContain("任务周期包含不支持的字段：timezone");
    // 后端每一条排期错误都点名"周期"——判定的正是这个域关键词。
    for (const scheduleMessage of [
      "任务周期必须是合法的 JSON。",
      "任务周期必须是一个对象。",
      "一次性事项不属于任务，任务只支持周期安排。",
      "任务周期的 kind 必须是 daily / weekly / monthly。",
      "weekly 周期的 weekday 必须是 1–7（1 = 周一）。",
      "monthly 周期的 day 必须是 1–28。",
    ]) {
      expect(
        createTaskErrorText(errorWith(400, "validation_error", scheduleMessage)),
      ).toBe(`这个安排不合法：${scheduleMessage}`);
    }
  });

  it("validation_error（非排期域）：同一个码也承载 title/commitment——原样冒错，**不**贴排期前缀", () => {
    // 后端 `storage/sqlite_task_repository.py:243-251`（`_validate_text`）的真实文案。
    const titleMessage = "Task title exceeds 200 characters.";
    const titleText = createTaskErrorText(
      errorWith(400, "validation_error", titleMessage),
    );
    // ① 后端 message 必须可见（绝不静默）；② 不得被错报成排期问题（F1）。
    expect(titleText).toBe(titleMessage);
    expect(titleText).not.toContain("这个安排不合法");
    expect(titleText).toContain("200");

    const commitmentMessage = "Task commitment must not be empty.";
    const commitmentText = createTaskErrorText(
      errorWith(400, "validation_error", commitmentMessage),
    );
    expect(commitmentText).toBe(commitmentMessage);
    expect(commitmentText).not.toContain("这个安排不合法");

    // 来源锚点的 validation_error 同理：不带排期前缀。
    const anchorMessage = "Task source conversation and turn are required.";
    expect(
      createTaskErrorText(errorWith(400, "validation_error", anchorMessage)),
    ).toBe(anchorMessage);
  });

  it("409：如实说「后端拒绝」，并带出后端文案", () => {
    expect(
      createTaskErrorText(errorWith(409, "conflict", "Already running.")),
    ).toBe("后端拒绝了这次创建（可能已经存在同样的安排）：Already running.");
  });

  it("其它码 / 网络失败：也各有可见文案（绝不静默）", () => {
    expect(createTaskErrorText(errorWith(500, "internal_error", "服务器炸了"))).toBe(
      "创建没有成功：服务器炸了",
    );
    expect(createTaskErrorText(new Error("boom"))).toBe(
      "创建没有成功：连不上本地服务，请确认 API 已启动。",
    );
    expect(createTaskErrorText(null)).toBe(
      "创建没有成功：连不上本地服务，请确认 API 已启动。",
    );
  });
});

/* ================================================================================================
 * 批次 D Task 1：编辑安排的**纯逻辑**（预填 / 组包 / 错误映射）。
 *
 * 编辑与新建共用同一份 `TaskFormValues`、同一份 `validateTaskForm`，区别只在
 * ① 预填（从 `task_json.schedule` 读回）② 组包（`PATCH` 的允许面）③ 错误映射
 * （404/409 的真实原因与新建不同）。
 * ============================================================================================== */

const summary = (overrides: Partial<TaskSummary> = {}): TaskSummary => ({
  id: "task_1",
  title: "每周发布检查",
  commitment: "检查发布流水线",
  schedule: { kind: "weekly", weekday: 3, time: "07:30" },
  scheduleDescription: "每周三 07:30",
  status: "active",
  sourceConversationId: "conv_1",
  sourceTurnId: "run_1",
  sourceProposalId: null,
  createdAt: "2026-09-01T00:00:00Z",
  updatedAt: "2026-09-01T00:00:00Z",
  cancelledAt: null,
  ...overrides,
});

describe("taskFormValuesFromTask：编辑表单的预填（逐字段读回，不做映射）", () => {
  it("weekly：kind/time/weekday 逐字段回填，day 留空", () => {
    expect(
      taskFormValuesFromTask(
        summary({ schedule: { kind: "weekly", weekday: 3, time: "07:30" } }),
      ),
    ).toEqual({
      title: "每周发布检查",
      commitment: "检查发布流水线",
      kind: "weekly",
      time: "07:30",
      weekday: "3",
      day: "",
    });
  });

  it("monthly：day 回填、weekday 留空（控件拿到的是字符串）", () => {
    const values = taskFormValuesFromTask(
      summary({ schedule: { kind: "monthly", day: 12, time: "09:00" } }),
    );
    expect(values.kind).toBe("monthly");
    expect(values.day).toBe("12");
    expect(values.weekday).toBe("");
    expect(typeof values.weekday).toBe("string");
  });

  it("daily：weekday/day 都是空串（与新建表单的「刻意留空」一致）", () => {
    const values = taskFormValuesFromTask(
      summary({ schedule: { kind: "daily", time: "23:59" } }),
    );
    expect(values).toEqual({
      title: "每周发布检查",
      commitment: "检查发布流水线",
      kind: "daily",
      time: "23:59",
      weekday: "",
      day: "",
    });
  });

  it("回填出来的值能**原样通过**校验并组包回同一个 schedule（往返闭环）", () => {
    const task = summary();
    const values = taskFormValuesFromTask(task);
    expect(validateTaskForm(values)).toEqual({});
    expect(buildUpdateTaskPayload(values).schedule).toEqual(task.schedule);
  });
});

describe("buildUpdateTaskPayload：PATCH 的允许面（恰好 3 个键）", () => {
  it("weekly：键集合只有 title/commitment/schedule，schedule 形状随频度", () => {
    const payload = buildUpdateTaskPayload(
      values({ kind: "weekly", time: "07:30", weekday: "3" }),
    );
    expect(payload).toEqual({
      title: "每周发布检查",
      commitment: "检查发布流水线",
      schedule: { kind: "weekly", time: "07:30", weekday: 3 },
    });
    expect(Object.keys(payload).sort()).toEqual([
      "commitment",
      "schedule",
      "title",
    ]);
    // `status` / `source*` / `createdAt` **不是**允许面（后端 extra="forbid"）。
    expect(payload).not.toHaveProperty("status");
    expect(payload).not.toHaveProperty("sourceConversationId");
    expect(typeof payload.schedule?.weekday).toBe("number");
  });

  it("daily / monthly：schedule 只带该频度允许的键", () => {
    expect(
      buildUpdateTaskPayload(values({ kind: "daily" })).schedule,
    ).toEqual({ kind: "daily", time: "09:00" });
    expect(
      buildUpdateTaskPayload(values({ kind: "monthly", day: "12" })).schedule,
    ).toEqual({ kind: "monthly", time: "09:00", day: 12 });
  });

  it("title/commitment/time 去首尾空白", () => {
    const payload = buildUpdateTaskPayload(
      values({ title: "  标题  ", commitment: "  内容  ", time: " 07:30 " }),
    );
    expect(payload.title).toBe("标题");
    expect(payload.commitment).toBe("内容");
    expect(payload.schedule?.time).toBe("07:30");
  });
});

describe("updateTaskErrorText：编辑失败的逐类归因", () => {
  it("404：如实说这条安排已经不存在了（可能已在别处删除）", () => {
    const text = updateTaskErrorText(
      errorWith(404, "not_found", "Task not found: task_1"),
    );
    expect(text).toBe("这条安排已经不存在了（可能已在别处删除），编辑没有生效。");
  });

  it("409：取消是终态，不能再编辑", () => {
    const text = updateTaskErrorText(
      errorWith(409, "invalid_state", "Task task_1 is cancelled and cannot be updated."),
    );
    expect(text).toBe("现在不能编辑：这条安排已经取消，取消是终态。");
  });

  it("400 validation_error（排期域）：贴「这个安排不合法」前缀并带出后端文案", () => {
    const scheduleMessage = "一次性事项不属于任务，任务只支持周期安排。";
    expect(
      updateTaskErrorText(errorWith(400, "validation_error", scheduleMessage)),
    ).toBe(`这个安排不合法：${scheduleMessage}`);
  });

  it("400 validation_error（字段级）：原样冒错，不贴排期前缀", () => {
    const titleMessage = "Task title exceeds 200 characters.";
    const text = updateTaskErrorText(
      errorWith(400, "validation_error", titleMessage),
    );
    expect(text).toBe(titleMessage);
    expect(text).not.toContain("这个安排不合法");
  });

  it("其余（含网络）：如实带出状态码与后端 message，绝不静默", () => {
    expect(
      updateTaskErrorText(errorWith(500, "internal_error", "服务器炸了")),
    ).toBe("编辑没有成功（HTTP 500）：服务器炸了");
    expect(updateTaskErrorText(new Error("boom"))).toBe(
      "编辑没有成功：连不上本地服务，请确认 API 已启动。",
    );
  });
});
