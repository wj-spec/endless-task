import { describe, expect, it } from "vitest";
import { ApiClientError } from "../chat/api";
import type { TaskSource } from "./taskSource";
import {
  REMINDER_CREATE_ERROR_TEXT,
  buildCreateReminderPayload,
  createReminderErrorText,
  emptyReminderFormValues,
  validateReminderForm,
} from "./reminderForm";

/**
 * 批次 D Task 1：提醒表单的**纯逻辑**三件事，逐条钉住——
 * ① 组包：**恰好**契约里那 5 个键 + `dueAt` 恰好 `{kind:"once", at}`（后端
 *    `extra="forbid"`，多一个键就 400）；
 * ② 提交前校验：空标题 / 空内容 / 空时间 / 形状错 / **日期不存在**；
 * ③ 错误映射：后端 5 个 `missing_*` 码 + `reminder_due_in_past`（R-D3 的新码）
 *    + `validation_error`（多用途桶：只有"提醒周期"域才配前缀）+ 409/网络。
 *
 * 与 `taskForm.test.ts` 分开的理由就是词表分开：提醒只有 `once`、
 * 时间形如 `YYYY-MM-DDTHH:MM`；那边**故意不含 `once`**。
 */
const SOURCE: TaskSource = { conversationId: "conv_1", turnId: "run_1" };

const values = (
  overrides: Partial<ReturnType<typeof emptyReminderFormValues>> = {},
) => ({
  ...emptyReminderFormValues(),
  title: "给妈妈打电话",
  commitment: "打个电话问问家里情况",
  at: "2026-09-20T15:00",
  ...overrides,
});

const errorWith = (status: number, code: string, message: string) =>
  new ApiClientError({ ok: false, status } as Response, {
    error: { code, message },
  });

describe("reminderForm：组包（恰好 5 个键 + dueAt 契约）", () => {
  it("payload 的键集合与 dueAt 形状完全对齐后端 CreateReminderBody", () => {
    const payload = buildCreateReminderPayload(values(), SOURCE);
    expect(payload).toEqual({
      title: "给妈妈打电话",
      commitment: "打个电话问问家里情况",
      dueAt: { kind: "once", at: "2026-09-20T15:00" },
      conversationId: "conv_1",
      turnId: "run_1",
    });
    expect(Object.keys(payload).sort()).toEqual([
      "commitment",
      "conversationId",
      "dueAt",
      "title",
      "turnId",
    ]);
    // 提醒**只有** once：`dueAt` 里绝不能出现任务的 kind/time。
    expect(Object.keys(payload.dueAt).sort()).toEqual(["at", "kind"]);
    expect(payload.dueAt.kind).toBe("once");
  });

  it("本地墙钟字符串**原样**送（时区归一在后端，前端不自己转）", () => {
    const payload = buildCreateReminderPayload(
      values({ at: " 2026-01-02T03:04 " }),
      SOURCE,
    );
    // 只去首尾空白，不改时区、不改格式（后端 `_ONCE_PATTERN` + `reminder_due_to_utc_iso`）。
    expect(payload.dueAt.at).toBe("2026-01-02T03:04");
  });

  it("title/commitment 去首尾空白（后端按 trim 后的长度判空/上限）", () => {
    const payload = buildCreateReminderPayload(
      values({ title: "  标题  ", commitment: "  内容  " }),
      SOURCE,
    );
    expect(payload.title).toBe("标题");
    expect(payload.commitment).toBe("内容");
  });
});

describe("reminderForm：提交前校验", () => {
  it("空值 / 只有空白：字段级可见文案", () => {
    const errors = validateReminderForm({ title: "  ", commitment: "", at: "" });
    expect(errors.title).toBe("请填写提醒标题。");
    expect(errors.commitment).toBe("请填写到点要提醒你的内容。");
    expect(errors.at).toBe("请填写提醒时间。");
  });

  it("合法输入：无错误", () => {
    expect(validateReminderForm(values())).toEqual({});
  });

  it.each([
    ["缺 T 分隔符", "2026-09-20 15:00"],
    ["只有日期", "2026-09-20"],
    ["24 小时制越界", "2026-09-20T24:00"],
    ["分钟越界", "2026-09-20T15:60"],
    ["带时区后缀（后端不收）", "2026-09-20T15:00Z"],
    ["不是 ISO", "明天下午三点"],
  ])("%s：形状错 → 字段级文案", (_label, at) => {
    expect(validateReminderForm(values({ at })).at).toContain(
      "YYYY-MM-DDTHH:MM",
    );
  });

  it("形状对但日期不存在（2026-02-30）：也挡在提交前", () => {
    // 后端 `parse_reminder_due` 会回「提醒周期的日期不存在。」；前端对齐同一判据。
    expect(validateReminderForm(values({ at: "2026-02-30T09:00" })).at).toContain(
      "YYYY-MM-DDTHH:MM",
    );
  });

  it("**不**在本地判「已过去」：过去时刻放行，交由后端指名 400", () => {
    // 边界（按分钟、按 UTC 归一）只有后端一处权威；前端不抄第二份"现在"。
    expect(validateReminderForm(values({ at: "2020-01-01T00:00" }))).toEqual({});
  });
});

describe("reminderForm：后端错误的显式映射", () => {
  it("5 个 missing_* 各有可见中文文案（点名到字段）", () => {
    const cases: Array<[string, string]> = [
      ["missing_title", "请填写提醒标题。"],
      ["missing_commitment", "请填写到点要提醒你的内容。"],
      ["missing_due_at", "请填写提醒时间。"],
      [
        "missing_source_conversation",
        "没有可作为来源的当前会话，创建没有提交。",
      ],
      ["missing_source_turn", "没有可作为来源的当前轮次，创建没有提交。"],
    ];
    for (const [code, text] of cases) {
      expect(REMINDER_CREATE_ERROR_TEXT[code]).toBe(text);
      expect(createReminderErrorText(errorWith(400, code, "dueAt 是必填项。"))).toBe(
        text,
      );
    }
  });

  it("reminder_due_in_past（R-D3 的新码）→ 如实说「时间已经过去」", () => {
    expect(
      createReminderErrorText(
        errorWith(400, "reminder_due_in_past", "提醒时间已过去（…）。"),
      ),
    ).toBe("提醒时间已经过去了，请改成还没到的时刻（后端按当前分钟判定）。");
  });

  it("validation_error（提醒排期域）：贴前缀并带上后端指名 message", () => {
    const message = "提醒周期的 at 必须是 YYYY-MM-DDTHH:MM。";
    const text = createReminderErrorText(
      errorWith(400, "validation_error", message),
    );
    expect(text).toBe(`这个提醒时间不合法：${message}`);
    expect(text).toContain(message);
  });

  it("validation_error（非排期域）：原样冒错，**不**贴提醒时间前缀", () => {
    const titleMessage = "Reminder title exceeds 200 characters.";
    const text = createReminderErrorText(
      errorWith(400, "validation_error", titleMessage),
    );
    expect(text).toBe(titleMessage);
    expect(text).not.toContain("这个提醒时间不合法");
  });

  it("409 / 其它码 / 网络：各有可见文案（绝不静默）", () => {
    expect(
      createReminderErrorText(errorWith(409, "invalid_state", "已取消。")),
    ).toBe("后端拒绝了这次创建（这条提醒的状态已经变了）：已取消。");
    expect(createReminderErrorText(errorWith(500, "internal_error", "炸了"))).toBe(
      "创建没有成功：炸了",
    );
    expect(createReminderErrorText(new Error("boom"))).toBe(
      "创建没有成功：连不上本地服务，请确认 API 已启动。",
    );
    expect(createReminderErrorText(null)).toBe(
      "创建没有成功：连不上本地服务，请确认 API 已启动。",
    );
  });
});
