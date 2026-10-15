import { ApiClientError } from "../chat/api";
import type { CreateReminderInput } from "../chat/apiTypes";
import type { TaskSource } from "./taskSource";

/**
 * 批次 D Task 1：「新建提醒」表单的**纯逻辑**（校验 / 组包 / 错误映射）。
 *
 * 为什么不复用 `taskForm.ts`：那边是**任务**的词表，`TASK_SCHEDULE_KINDS` **故意
 * 不含 `once`**（一次性事项不是任务），`TIME_PATTERN` 是 `HH:MM`。提醒恰好相反：
 * 只有 `once`，时刻是 `YYYY-MM-DDTHH:MM`。硬把两套塞进一个词表只会让"这个安排
 * 到底是不是任务"变得说不清——所以提醒有自己的词表，两边各自对齐后端一处。
 *
 * 抽成纯函数的理由与 `taskForm.ts` 相同：逐字段契约、错误映射、提交前校验都能被
 * 单测直接钉住，不用起后端、也不依赖 DOM。
 */

export type ReminderFormValues = {
  title: string;
  commitment: string;
  /** 本地墙钟 `YYYY-MM-DDTHH:MM`；空串 = 没填。 */
  at: string;
};

export type ReminderFormField = "title" | "commitment" | "at";

export type ReminderFormFieldErrors = Partial<Record<ReminderFormField, string>>;

/**
 * 和后端 `domain/task_schedule.py` 的 `_ONCE_PATTERN` 同一个口径：
 * `^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$`（**本地墙钟**，不写时区后缀）。
 */
export const REMINDER_DUE_PATTERN = /^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d$/;

/** 表单初始值：只给一个空的时刻，让用户自己填（提醒没有"合理默认"）。 */
export const emptyReminderFormValues = (): ReminderFormValues => ({
  title: "",
  commitment: "",
  at: "",
});

/**
 * 日期是否**真实存在**（`2026-02-30` 形状对但日子不存在）。
 *
 * 后端 `parse_reminder_due` 会对 `at` 走 `datetime.strptime`，日期不存在时回
 * `提醒周期的日期不存在。`。这一层对齐同一个判据，好让"拦在提交前"和"后端报错"
 * 说的是同一件事。
 */
const isRealLocalDate = (value: string): boolean => {
  const [year, month, day] = value.slice(0, 10).split("-").map(Number);
  if (!year || !month || !day) return false;
  const moment = new Date(year, month - 1, day);
  return (
    moment.getFullYear() === year &&
    moment.getMonth() === month - 1 &&
    moment.getDate() === day
  );
};

/**
 * 提交前校验。返回空对象 = 通过。
 *
 * **刻意不在这里判"时间是否已过去"**：那一条的权威边界在后端
 * （`api/routes/reminders.py:reminder_due_has_passed`，按分钟、按 UTC 归一后比较），
 * 前端再抄一份"现在"只会有两种口径。这里只挡"形状/真实性"，过去时刻由后端指名
 * 400（`reminder_due_in_past`），表单把它翻成可见文案。
 */
export const validateReminderForm = (
  values: ReminderFormValues,
): ReminderFormFieldErrors => {
  const errors: ReminderFormFieldErrors = {};
  if (!values.title.trim()) errors.title = "请填写提醒标题。";
  if (!values.commitment.trim()) {
    errors.commitment = "请填写到点要提醒你的内容。";
  }
  const at = values.at.trim();
  if (!at) {
    errors.at = "请填写提醒时间。";
  } else if (!REMINDER_DUE_PATTERN.test(at) || !isRealLocalDate(at)) {
    errors.at = "提醒时间要写成 YYYY-MM-DDTHH:MM（本地时间），例如 2026-09-20T15:00。";
  }
  return errors;
};

/**
 * 组包：**恰好**契约里那 5 个键，`dueAt` 恰好 `{kind:"once", at}`。
 *
 * `at` 原样送本地墙钟字符串（**不做时区换算**）：归一成 UTC 是后端
 * `reminder_due_to_utc_iso` 的职责，而调度是字符串比较——前端自己转一遍只会
 * 多一个能出错的地方。
 */
export const buildCreateReminderPayload = (
  values: ReminderFormValues,
  source: TaskSource,
): CreateReminderInput => ({
  title: values.title.trim(),
  commitment: values.commitment.trim(),
  dueAt: { kind: "once", at: values.at.trim() },
  conversationId: source.conversationId,
  turnId: source.turnId,
});

/**
 * 后端指名 400 码 → 可见文案。码来自 `api/routes/reminders.py` 的
 * `missing_*` 一族与 R-D3 新增的 `reminder_due_in_past`。
 */
export const REMINDER_CREATE_ERROR_TEXT: Readonly<Record<string, string>> = {
  missing_title: "请填写提醒标题。",
  missing_commitment: "请填写到点要提醒你的内容。",
  missing_due_at: "请填写提醒时间。",
  reminder_due_in_past: "提醒时间已经过去了，请改成还没到的时刻（后端按当前分钟判定）。",
  missing_source_conversation: "没有可作为来源的当前会话，创建没有提交。",
  missing_source_turn: "没有可作为来源的当前轮次，创建没有提交。",
};

/**
 * `validation_error` 与任务域同形：它是**多用途**码——`parse_reminder_due` 的
 * 词表错、以及仓储 `_validate_text` 的 title/commitment 非空与长度**共用**它。
 * 后端每一条提醒排期错误都点名"提醒周期"，字段级错误一条都不带；于是按**域
 * 关键词**判定，认不出来时一律**不贴前缀**（宁可少一个框架词，也不替排期背锅）。
 */
export const REMINDER_VALIDATION_DUE_MARKER = "提醒周期";

export const createReminderErrorText = (error: unknown): string => {
  if (error instanceof ApiClientError) {
    const named = REMINDER_CREATE_ERROR_TEXT[error.code];
    if (named) return named;
    if (error.code === "validation_error") {
      if (error.message.includes(REMINDER_VALIDATION_DUE_MARKER)) {
        return `这个提醒时间不合法：${error.message}`;
      }
      // 非排期域（title/commitment 的非空与长度）：原样呈现后端指名 message。
      return error.message;
    }
    if (error.status === 409) {
      return `后端拒绝了这次创建（这条提醒的状态已经变了）：${error.message}`;
    }
    return `创建没有成功：${error.message}`;
  }
  return "创建没有成功：连不上本地服务，请确认 API 已启动。";
};
