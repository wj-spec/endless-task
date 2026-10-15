import { afterEach, describe, expect, it, vi } from "vitest";
import { chatApi } from "./api";
import type {
  CreateReminderInput,
  RuntimeV2ConversationReference,
  RuntimeV2MessageResponse,
  UpdateTaskInput,
} from "./apiTypes";

/**
 * 批次 D 的 HTTP **接线**测试（D1 复审 I-1 的覆盖洞）。
 *
 * 为什么单独有这份文件：批次 D 新增的两个客户端方法（`createReminder`、
 * `updateTask`）此前**在任何层级都没有接线覆盖**——唯一驱动它们的
 * `ScheduledTasks.test.tsx` 用 `vi.mock("../chat/api", …)` 把整个模块换掉了，
 * `taskForm.test.ts` / `reminderForm.test.ts` 只用到 `ApiClientError`，e2e 对
 * 新表单零命中。实测（D1 复审）：把 `updateTask` 的路径改成 `/task/${taskId}`、
 * 方法改成 `PUT`，把 `createReminder` 的路径改成 `/reminder-TYPO` 之后，
 * `src/features/tasks` 的 6 文件 / 115 条用例**仍然全绿**——也就是说路径 / 方法 /
 * 请求体任一处写错，四道门禁全绿，用户点「创建提醒」直接 404。
 *
 * 所以这份文件的规矩是：
 *
 * - **只 stub 全局 `fetch`**（`vi.stubGlobal`），绝不 `vi.mock("../chat/api")`
 *   ——后者等于把被测对象整块换掉，什么都测不到；
 * - 断言落在**实际 fetch 收到的 `{url, method, body}`** 上（记录后逐项断言），
 *   不是「被调用过」；
 * - URL 期望值用**字面路径**（`/reminders`、`/tasks/<id>`）拼上本文件自己按
 *   `api.ts` 同一条 env 推导规则算出的 base——**不调用被测函数反推期望值**，
 *   否则是重言式；
 * - `vi.stubGlobal` 配 `afterEach` 的 `vi.unstubAllGlobals()`，避免污染同进程
 *   的其它测试文件。
 */

/** 与 `api.ts` 里 `configuredBaseUrl` 同一条推导（不含末尾 `/`）。 */
const BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

/** 字面任务 id：路径断言里必须出现**真实 id**，而不是形状相似的占位符。 */
const TASK_ID = "task-d1wiring7f3a";

type CapturedRequest = {
  url: string;
  method: string;
  body: unknown;
  headers: Record<string, string>;
};

/**
 * 用一次 `fetch` 响应替换全局 fetch，并**记录**调用方实际发出去的东西。
 * 记录发生在被测代码与网络之间，所以断言的对象就是「线上真正会发出去的请求」。
 */
function captureFetch(payload: unknown, status = 200): CapturedRequest[] {
  const captured: CapturedRequest[] = [];
  vi.stubGlobal(
    "fetch",
    (input: unknown, init?: RequestInit): Promise<Response> => {
      captured.push({
        url: String(input),
        method: (init?.method ?? "GET").toUpperCase(),
        body:
          typeof init?.body === "string" ? JSON.parse(init.body) : init?.body,
        headers: { ...((init?.headers ?? {}) as Record<string, string>) },
      });
      return Promise.resolve(
        new Response(JSON.stringify(payload), {
          status,
          headers: { "Content-Type": "application/json" },
        }),
      );
    },
  );
  return captured;
}

afterEach(() => {
  vi.unstubAllGlobals();
  // 下面有一条用例会 `vi.stubEnv` + `vi.resetModules` + 动态 import；
  // 不还原 env 会把非空 base 泄漏给后面的用例（`vi.unstubAllGlobals` 不管 env）。
  vi.unstubAllEnvs();
});

describe("chatApi 接线（POST /reminders）", () => {
  it("createReminder 发出 POST /reminders，请求体恰好是契约那 5 个键", async () => {
    const captured = captureFetch({ reminder: { id: "rem-1" } }, 201);
    const input: CreateReminderInput = {
      title: "交周报",
      commitment: "把本周周报发到项目群里",
      dueAt: { kind: "once", at: "2026-09-08T09:30" },
      conversationId: "conv-d1wiring",
      turnId: "turn-d1wiring",
    };

    // 返回值：响应体是 `{ reminder: … }`，这一层必须**解包到 `reminder` 字段**。
    // 期望值刻意用与入参无关的可辨识值 `rem-1`：写成 `resolves.toEqual(payload)`
    // 或 `resolves.toEqual(input)` 都成了重言式，这里等于没测。
    // 调用先收进 `result`，断言刻意排在请求面断言**之后**：把
    // `.then((r) => r.reminder)` 破坏掉时，上面那批旧断言会先跑完且全绿，
    // 红点只落在最后这一行——直接证明它是**新增的检出力**，不是旧断言的重复。
    const result = await chatApi.createReminder(input);

    expect(captured).toHaveLength(1);
    const [{ url, method, body, headers }] = captured;

    // 字面路径 + 真实方法与 base：路径写错（例如 /reminder-TYPO）、
    // 方法写错（例如 PUT）都在这里立刻红。
    expect(url).toBe(`${BASE}/reminders`);
    expect(method).toBe("POST");

    // 请求体：恰好这 5 个键，一个不多一个不少（后端 `extra="forbid"`）。
    expect(Object.keys(body as object).sort()).toEqual([
      "commitment",
      "conversationId",
      "dueAt",
      "title",
      "turnId",
    ]);
    // 值也必须原样送出：`dueAt.at` 是本地墙钟，这一层不做时区换算。
    expect(body).toEqual({
      title: "交周报",
      commitment: "把本周周报发到项目群里",
      dueAt: { kind: "once", at: "2026-09-08T09:30" },
      conversationId: "conv-d1wiring",
      turnId: "turn-d1wiring",
    });
    expect(Object.keys((body as CreateReminderInput).dueAt).sort()).toEqual([
      "at",
      "kind",
    ]);

    // 有 body 就必须带 JSON Content-Type（否则后端 415/400）。
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers.Accept).toBe("application/json");

    // 响应解包（复审 I-1'）：取的是 `response.reminder`，不是整个响应体。
    expect(result).toEqual({ id: "rem-1" });
  });
});

describe("chatApi 接线（PATCH /tasks/{id}）", () => {
  it("updateTask 发出 PATCH /tasks/<真实 id>，请求体只含允许面的键", async () => {
    const captured = captureFetch({ task: { id: TASK_ID } });
    const input: UpdateTaskInput = {
      title: "改成每周交周报",
      commitment: "每周五 18:00 前交周报",
      schedule: { kind: "weekly", time: "18:00", weekday: 5 },
    };

    // 返回值：响应体是 `{ task: … }`，必须解包到 `task`（取的是 `response.task`，
    // 不是整个响应体）；`TASK_ID` 是可辨识值，回退成 `{}` 或整个响应体都会红。
    // 与上面那条同理，放在请求面断言之后，红点只会落在最后这一行。
    const result = await chatApi.updateTask(TASK_ID, input);

    expect(captured).toHaveLength(1);
    const [{ url, method, body, headers }] = captured;

    // 路径里必须出现**真实 id**：写成 `/task/${taskId}`（单数）或
    // `/tasks` （丢了 id）都会在这里红。
    expect(url).toBe(`${BASE}/tasks/${TASK_ID}`);
    expect(method).toBe("PATCH");

    // 允许面只有 title / commitment / schedule 三个键。
    expect(Object.keys(body as object).sort()).toEqual([
      "commitment",
      "schedule",
      "title",
    ]);
    expect(body).toEqual({
      title: "改成每周交周报",
      commitment: "每周五 18:00 前交周报",
      schedule: { kind: "weekly", time: "18:00", weekday: 5 },
    });

    // 与 createReminder 同一口径（复审 M-3）：有 body 就必须带 JSON Content-Type，
    // 否则后端 415/400。
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers.Accept).toBe("application/json");

    // 响应解包（复审 I-1'）：取的是 `response.task`。
    expect(result).toEqual({ id: TASK_ID });
  });

  it("updateTask 偏置用例：只传 commitment 时不得补出任何别的键", async () => {
    const captured = captureFetch({ task: { id: TASK_ID } });

    // 编辑表单「只改了承诺」这一路：后端 `extra="forbid"`，多一个键就 400，
    // 所以这里必须逐字只送一个键（不能把 title/schedule 用 undefined 带出去）。
    const result = await chatApi.updateTask(TASK_ID, {
      commitment: "改成每天交",
    });

    expect(captured).toHaveLength(1);
    const [{ url, method, body, headers }] = captured;

    expect(url).toBe(`${BASE}/tasks/${TASK_ID}`);
    expect(method).toBe("PATCH");
    expect(Object.keys(body as object)).toEqual(["commitment"]);
    expect(body).toEqual({ commitment: "改成每天交" });
    // headers 两条与上面那条同形（M-3）。
    expect(headers["Content-Type"]).toBe("application/json");
    expect(headers.Accept).toBe("application/json");

    // 响应解包（复审 I-1'）：这条偏置路径同样必须解到 `task`。
    expect(result).toEqual({ id: TASK_ID });
  });
});

describe("chatApi 接线（base URL 推导）", () => {
  /**
   * 复审 M-2：上面的 `BASE` 与实现侧（`api.ts:94`）在本地都读同一个
   * `import.meta.env.VITE_API_BASE_URL`，而仓库里没有 `.env*` ⇒ 两边都退化成
   * `""`，`${BASE}/reminders` 实际等价于字面 `/reminders`。于是**把实现侧的 env
   * 名改掉**（测试侧这行不改），两边仍然都是空串，上面三条照样全绿——这是个盲点。
   *
   * 这一条把非空 base 的侧面钉住：把 env 塞成非空、重置模块注册表后**动态**
   * 导入被测模块（只有它能读到新 env），期望值写**字面** `http://api.test/reminders`
   * ——不重推、不调用被测函数反算。实现侧 env 名一旦被换（或末尾 `/` 的裁剪被删），
   * 这里立刻红。
   */
  it("base 取自 VITE_API_BASE_URL：env 名被换掉就红", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "http://api.test/");
    vi.resetModules();
    const { chatApi: freshChatApi } = await import("./api");

    const captured = captureFetch({ reminder: { id: "rem-2" } }, 201);
    await freshChatApi.createReminder({
      title: "交周报",
      commitment: "把本周周报发到项目群里",
      dueAt: { kind: "once", at: "2026-09-08T09:30" },
      conversationId: "conv-d1wiring",
      turnId: "turn-d1wiring",
    });

    expect(captured).toHaveLength(1);
    expect(captured[0].url).toBe("http://api.test/reminders");
  });
});


/**
 * C3b：`@` 引用真的进了 `POST /api/v2/conversations/{id}/messages` 的 body。
 *
 * 为什么必须在这一层断言：调用方（`useChatApplication`）用的是**位置参数**
 * `(conversationId, content, laneId, idempotencyKey, references)`——`references`
 * 写错位置、或者把 `Idempotency-Key` 挤掉，四个门禁都会全绿，而用户侧的表现是
 * 「引用没生效」或「重复提交」。所以这里把**线上真正发出去的请求**逐项钉住。
 */
describe("chatApi 接线（POST /api/v2/conversations/{id}/messages 的 references）", () => {
  const CONVERSATION_ID = "conv-c3b-wiring";
  const LANE_ID = "lane-c3b-wiring";
  /** 形状与 `crypto.randomUUID()` 一致的固定值（该参数的类型被 infer 成 UUID 模板字面量）。 */
  const IDEMPOTENCY_KEY = "8f13a0b1-2c3d-4e5f-8a9b-0c1d2e3f4a5b";

  const references: RuntimeV2ConversationReference[] = [
    {
      kind: "conversation",
      conversationId: "conv-c3b-other",
      mode: "recent",
      recentTurns: 5,
      entryIds: [],
    },
  ];

  it("带引用：请求体恰好是 content / laneId / references，且 Idempotency-Key 没被挤掉", async () => {
    const captured = captureFetch({
      conversationId: CONVERSATION_ID,
      laneId: LANE_ID,
      runId: "run-1",
      userMessageId: "msg-1",
      referenceTokenEstimate: 1234,
    });

    await chatApi.createRuntimeV2Message(
      CONVERSATION_ID,
      "把那段上下文一起看",
      LANE_ID,
      IDEMPOTENCY_KEY,
      references,
    );

    expect(captured).toHaveLength(1);
    const [{ url, method, body, headers }] = captured;
    expect(url).toBe(`${BASE}/api/v2/conversations/${CONVERSATION_ID}/messages`);
    expect(method).toBe("POST");
    // 幂等键是**第 4 个位置参数**：它一旦和 references 错位，这条立刻红。
    expect(headers["Idempotency-Key"]).toBe(IDEMPOTENCY_KEY);
    // 键集合一个不多一个不少（后端 `extra="forbid"`）。
    expect(Object.keys(body as object).sort()).toEqual([
      "content",
      "laneId",
      "references",
    ]);
    expect(body).toEqual({
      content: "把那段上下文一起看",
      laneId: LANE_ID,
      // 判别联合的成员形状逐字送出去（`entryIds` 是一期预留，送空数组）。
      references: [
        {
          kind: "conversation",
          conversationId: "conv-c3b-other",
          mode: "recent",
          recentTurns: 5,
          entryIds: [],
        },
      ],
    });
  });

  it("不带引用：请求体仍然只有 content / laneId（老客户端行为逐字不变）", async () => {
    const captured = captureFetch({
      conversationId: CONVERSATION_ID,
      laneId: LANE_ID,
      runId: "run-2",
      userMessageId: "msg-2",
    });

    await chatApi.createRuntimeV2Message(
      CONVERSATION_ID,
      "普通消息",
      LANE_ID,
      IDEMPOTENCY_KEY,
    );

    expect(captured).toHaveLength(1);
    const [{ body, headers }] = captured;
    expect(Object.keys(body as object).sort()).toEqual(["content", "laneId"]);
    expect(body).toEqual({ content: "普通消息", laneId: LANE_ID });
    expect(headers["Idempotency-Key"]).toBe(IDEMPOTENCY_KEY);
  });

  it("空数组与不传等价：不得出现 `references: []` 这种「看着有、其实没有」的键", async () => {
    const captured = captureFetch({
      conversationId: CONVERSATION_ID,
      laneId: LANE_ID,
      runId: "run-3",
      userMessageId: "msg-3",
    });

    await chatApi.createRuntimeV2Message(
      CONVERSATION_ID,
      "普通消息",
      LANE_ID,
      IDEMPOTENCY_KEY,
      [],
    );

    expect(captured).toHaveLength(1);
    expect(Object.keys(captured[0].body as object).sort()).toEqual([
      "content",
      "laneId",
    ]);
  });
});

describe("chatApi 接线（POST /api/v2/conversations/{id}/references/preview）", () => {
  it("预览走字面路径、body 只有 references，并把后端的 totalTokenEstimate 解出来", async () => {
    const captured = captureFetch({
      conversationId: "conv-c3b-preview",
      totalTokenEstimate: 4321,
      warnings: ["已选 3 条，超过默认上限 2 条"],
      items: [
        {
          conversationId: "conv-c3b-other",
          laneId: "lane-1",
          mode: "summary",
          recentTurns: 2,
          tokenEstimate: 4321,
          text: "投影文本",
        },
      ],
    });

    const result = await chatApi.previewRuntimeV2References("conv-c3b-preview", [
      { kind: "conversation", conversationId: "conv-c3b-other", mode: "summary" },
    ]);

    expect(captured).toHaveLength(1);
    const [{ url, method, body }] = captured;
    expect(url).toBe(
      `${BASE}/api/v2/conversations/conv-c3b-preview/references/preview`,
    );
    expect(method).toBe("POST");
    expect(Object.keys(body as object)).toEqual(["references"]);
    expect(body).toEqual({
      references: [
        {
          kind: "conversation",
          conversationId: "conv-c3b-other",
          mode: "summary",
        },
      ],
    });
    // 数字来自响应（前端不许自己算：R-C4 修订）。
    expect(result.totalTokenEstimate).toBe(4321);
    expect(result.warnings).toEqual(["已选 3 条，超过默认上限 2 条"]);
  });
});

describe("发送响应的 references 类型（M-2）", () => {
  it("发送响应只回 provenance：**不要求** `text`（`npm run build` 的 tsc 是这条的守卫）", () => {
    // 后端发送 route 回的是 `[dict(item.provenance) …]`（没有 `text`），只有**预览**
    // route 才把投影正文附上 ⇒ `RuntimeV2MessageResponse.references` 不能复用"要求
    // text: string"的预览类型。下面这个字面量一旦编译不过（有人把类型改回去），
    // `npm run build`（`tsc -b` 覆盖测试文件）就会红。
    const response: RuntimeV2MessageResponse = {
      conversationId: "conv-c3b-send",
      laneId: "lane-c3b-send",
      runId: "run-c3b-send",
      userMessageId: "msg-c3b-send",
      references: [
        { conversationId: "conv-c3b-other", mode: "summary", tokenEstimate: 1234 },
      ],
      referenceTokenEstimate: 1234,
      referenceWarnings: [],
    };
    expect(response.references?.[0]).not.toHaveProperty("text");
  });
});
