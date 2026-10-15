import { ApiClientError } from "../chat/api";
import type {
  CreateTaskInput,
  TaskSummary,
  UpdateTaskInput,
} from "../chat/apiTypes";
import type { TaskSource } from "./taskSource";

/**
 * 切片 10 T6：「新建定时任务」表单的**纯逻辑**（校验 / 组包 / 错误映射）。
 *
 * 抽成纯函数是为了让"逐字段契约""错误映射表""提交前校验"能被单测直接钉住，
 * 不用起后端、也不依赖 DOM。
 */

export type TaskScheduleKind = CreateTaskInput["schedule"]["kind"];

/**
 * `once`（一次性事项）**不是任务**：后端 `parse_task_schedule` 对它直接 400
 * （`一次性事项不属于任务，任务只支持周期安排。`），所以表单只提供这三种。
 */
export const TASK_SCHEDULE_KINDS: readonly TaskScheduleKind[] = [
  "daily",
  "weekly",
  "monthly",
];

export const TASK_SCHEDULE_KIND_LABEL: Record<TaskScheduleKind, string> = {
  daily: "每天",
  weekly: "每周",
  monthly: "每月",
};

/** 和后端 `_TIME_PATTERN = ^([01]\d|2[0-3]):[0-5]\d$` 同一个口径（不另立词表）。 */
const TIME_PATTERN = /^([01]\d|2[0-3]):[0-5]\d$/;

export type TaskFormValues = {
  title: string;
  commitment: string;
  kind: TaskScheduleKind;
  time: string;
  /** 周几（1–7）；空串 = 未选。仅 weekly 用。 */
  weekday: string;
  /** 每月几号（1–28）；空串 = 未选。仅 monthly 用。 */
  day: string;
};

export type TaskFormField = "title" | "commitment" | "time" | "weekday" | "day";

export type TaskFormFieldErrors = Partial<Record<TaskFormField, string>>;

export const TASK_WEEKDAY_OPTIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "1", label: "周一" },
  { value: "2", label: "周二" },
  { value: "3", label: "周三" },
  { value: "4", label: "周四" },
  { value: "5", label: "周五" },
  { value: "6", label: "周六" },
  { value: "7", label: "周日" },
];

/** 表单初始值：周期/时间给合理默认，`weekday`/`day` **刻意留空**要用户显式选。 */
export const emptyTaskFormValues = (): TaskFormValues => ({
  title: "",
  commitment: "",
  kind: "daily",
  time: "09:00",
  weekday: "",
  day: "",
});

/** 提交前校验。返回空对象 = 通过。 */
export const validateTaskForm = (values: TaskFormValues): TaskFormFieldErrors => {
  const errors: TaskFormFieldErrors = {};
  if (!values.title.trim()) errors.title = "请填写任务标题。";
  if (!values.commitment.trim()) errors.commitment = "请填写到点要执行的内容。";
  if (!TIME_PATTERN.test(values.time.trim())) {
    errors.time = "时间要写成 HH:MM（24 小时制），例如 09:00。";
  }
  if (values.kind === "weekly") {
    const weekday = Number(values.weekday);
    if (
      values.weekday.trim() === "" ||
      !Number.isInteger(weekday) ||
      weekday < 1 ||
      weekday > 7
    ) {
      errors.weekday = "请选择周几（周一至周日）。";
    }
  }
  if (values.kind === "monthly") {
    const day = Number(values.day);
    if (
      values.day.trim() === "" ||
      !Number.isInteger(day) ||
      day < 1 ||
      day > 28
    ) {
      errors.day = "请选择每月几号（1–28）。";
    }
  }
  return errors;
};

/**
 * 组包：**恰好**契约里那 5 个键，`schedule` 只带该频度允许的键
 * （后端 `parse_task_schedule` 对多余键报 `任务周期包含不支持的字段：…`）。
 */
export const buildCreateTaskPayload = (
  values: TaskFormValues,
  source: TaskSource,
): CreateTaskInput => {
  const schedule: CreateTaskInput["schedule"] = {
    kind: values.kind,
    time: values.time.trim(),
  };
  if (values.kind === "weekly") schedule.weekday = Number(values.weekday);
  if (values.kind === "monthly") schedule.day = Number(values.day);
  return {
    title: values.title.trim(),
    commitment: values.commitment.trim(),
    schedule,
    conversationId: source.conversationId,
    turnId: source.turnId,
  };
};

/**
 * 后端指名 400 码 → 可见文案。码来自 `api/routes/tasks.py` 的 5 个信封层 `missing_*`。
 * 展示上**保留后端语义**：`missing_*` 有固定中文文案；`validation_error` 用后端
 * domain 给的**指名 message 原样**（它已点出 kind/time/weekday/day/多余字段名），
 * 这里不重写第二份词表、也不吞。
 */
export const TASK_CREATE_ERROR_TEXT: Readonly<Record<string, string>> = {
  missing_title: "请填写任务标题。",
  missing_commitment: "请填写到点要执行的内容。",
  missing_schedule: "请选择执行周期。",
  missing_source_conversation: "没有可作为来源的当前会话，创建没有提交。",
  missing_source_turn: "没有可作为来源的当前轮次，创建没有提交。",
};

/**
 * `validation_error` 是**多用途**码（`domain/repositories.py:50-52`）：排期解析
 * （`domain/task_schedule.py`）用它，title/commitment 的非空与长度
 * （`storage/sqlite_task_repository.py:243-251`：
 * `Task title exceeds 200 characters.`）**也**用它。
 *
 * 所以"这个安排不合法"这个前缀**不能无条件贴**——否则一个超长标题会被错报成
 * 排期问题。判定方式：后端每一条排期错误都点名"周期"
 * （`任务周期必须是…` / `weekly 周期的 weekday …` / `monthly 周期的 day …`
 * / `一次性事项…任务只支持周期安排。`），而字段级错误一条都不带"周期"。
 * 于是按**域关键词**判定，而不是再抄一份文案词表；认不出来时一律**不贴前缀**
 * （宁可少一个框架词，也不替排期背锅）。
 */
export const TASK_VALIDATION_SCHEDULE_MARKER = "周期";

export const createTaskErrorText = (error: unknown): string => {
  if (error instanceof ApiClientError) {
    const named = TASK_CREATE_ERROR_TEXT[error.code];
    if (named) return named;
    if (error.code === "validation_error") {
      if (error.message.includes(TASK_VALIDATION_SCHEDULE_MARKER)) {
        return `这个安排不合法：${error.message}`;
      }
      // 非排期域（title/commitment 的非空与长度、来源锚点）：
      // 原样呈现后端指名 message，**不**贴排期前缀（错误归因），也**不**吞。
      return error.message;
    }
    if (error.status === 409) {
      return `后端拒绝了这次创建（可能已经存在同样的安排）：${error.message}`;
    }
    return `创建没有成功：${error.message}`;
  }
  return "创建没有成功：连不上本地服务，请确认 API 已启动。";
};

/* ------------------------------------------------------------------ 批次 D Task 1：任务编辑 */

/**
 * 把一条任务的当前排期**回填**成表单值（编辑表单的预填）。
 *
 * 逐字段对着 `task_json.schedule` 读，不需要任何映射或换算：`api/serialization.py`
 * 的 `task_schedule_json`（响应形状）与 `domain/task_schedule.py` 的
 * `serialize_task_schedule`（写入形状）**逐字段相同**（`{kind, time, weekday?, day?}`）。
 *
 * `weekday`/`day` 回填成**字符串**（表单控件是 `<select>` / `<input>`）；不适用或
 * 缺失时是空串（与 `emptyTaskFormValues` 的"刻意留空"一致，用户必须显式选）。
 * 防御性处理 `once`：任务不可能是提醒（后端 `parse_task_schedule` 直接 400），
 * 真拿到就如实回落到 `daily` 而不是崩掉。
 */
export const taskFormValuesFromTask = (task: TaskSummary): TaskFormValues => {
  const schedule = task.schedule;
  const kind: TaskScheduleKind =
    schedule.kind === "weekly" || schedule.kind === "monthly"
      ? schedule.kind
      : "daily";
  return {
    title: task.title,
    commitment: task.commitment,
    kind,
    time: schedule.time ?? "09:00",
    weekday: schedule.weekday === undefined ? "" : String(schedule.weekday),
    day: schedule.day === undefined ? "" : String(schedule.day),
  };
};

/**
 * 组包 `PATCH /tasks/{id}` 的请求体：**恰好**契约里那三个键（后端
 * `UpdateTaskBody` 是 `extra="forbid"`）。
 *
 * `schedule` 与 `POST /tasks` 用**同一个**形状：编辑表单把当前排期原样回填、
 * 原样回写是正常用法——后端按解析后的**语义**判断"排期到底变没变"，原样回写
 * 不会重置触发锚点（R-D7）。所以这里不做"只送改动字段"的差分，前端不承担
 * "哪里变了"的判定（那是语义，只有后端一处权威）。
 */
export const buildUpdateTaskPayload = (
  values: TaskFormValues,
): UpdateTaskInput => {
  const schedule: NonNullable<UpdateTaskInput["schedule"]> = {
    kind: values.kind,
    time: values.time.trim(),
  };
  if (values.kind === "weekly") schedule.weekday = Number(values.weekday);
  if (values.kind === "monthly") schedule.day = Number(values.day);
  return {
    title: values.title.trim(),
    commitment: values.commitment.trim(),
    schedule,
  };
};

/**
 * 编辑失败的**逐类**归因（复用 `TASK_CREATE_ERROR_TEXT` 与同一条
 * `validation_error` 多义桶判定）：
 *
 * - 404 `not_found`：这条安排已经不存在了（可能已在别处删除）；
 * - 409 `invalid_state`：已取消的安排不能再改（终态，与 pause/resume/cancel 对齐）；
 * - 400 `validation_error`：排期域贴"这个安排不合法"前缀，字段级错误原样冒错；
 * - 其余（含网络）：如实说、带出后端 message，绝不静默。
 */
export const updateTaskErrorText = (error: unknown): string => {
  if (error instanceof ApiClientError) {
    if (error.status === 404) {
      return "这条安排已经不存在了（可能已在别处删除），编辑没有生效。";
    }
    if (error.status === 409) {
      return "现在不能编辑：这条安排已经取消，取消是终态。";
    }
    const named = TASK_CREATE_ERROR_TEXT[error.code];
    if (named) return named;
    if (error.code === "validation_error") {
      if (error.message.includes(TASK_VALIDATION_SCHEDULE_MARKER)) {
        return `这个安排不合法：${error.message}`;
      }
      return error.message;
    }
    return `编辑没有成功（HTTP ${error.status}）：${error.message}`;
  }
  return "编辑没有成功：连不上本地服务，请确认 API 已启动。";
};
