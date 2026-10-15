// @vitest-environment jsdom
/**
 * E3/B 复审登记的 **I-1**：动作字符串的**下游**零守护。
 *
 * 那条链是
 *   `RuntimeStatusStrip`/`EscalationNotice`「继续（接上这条运行）」
 *     → `ChatWorkSurface.onResumeRun`
 *     → `App.tsx` 主区 `chat.resolveRuntimeRecovery(runId, "resume")`
 *     → `useMessageActions.resolveRuntimeRecovery`
 *     → `chatApi.resolveRuntimeV2Recovery(runId, action)`
 *     → `api.ts` 的 `POST /api/v2/runs/{runId}/recovery` 请求体 `{ action }`
 * 五段串起来的。复审实测：把 `useMessageActions.ts` 那一处写死成 `"retry"`（M7）、
 * 或把 `api.ts` 的请求体写死成 `"retry"`（M8），全量单测 `675 passed` + `build`
 * EXIT=0 —— **双绿**。也就是说这条链任意一处把动作名写错，用户点「继续（接上这条运行）」
 * 会静默变成另一种恢复动作，没有任何门禁会红。
 *
 * 上游已有守护**别重复**：`ChatWorkSurface.runResume.test.tsx` 渲染真组件但给回调打桩，
 * 钉的是"传给回调的实参"（`onResolveRuntimeRecovery("run_1", "resume")`），
 * 钉不到 `useMessageActions → api.ts → 请求体`。
 *
 * 所以这份文件的规矩是（姿势照抄 `apiWiring.test.ts` 的顶部注释）：
 *
 * - 渲染**真 `<App/>`**（真 `useChatApplication`、真 `ChatWorkSurface`、真
 *   `useMessageActions`），**绝不 `vi.mock("./api")`**——整模块替换正是前几批反复
 *   栽的"改坏了也不红"盲区；
 * - **只 stub 全局 `fetch`**，断言落在**实际 fetch 收到的 `{url, method, body}`** 上；
 * - URL 期望值用**字面路径**拼上本文件自己按 `api.ts` 同一条 env 推导规则算出的 base
 *   ——不调用被测函数反推期望值；
 * - 反向断言：**不可复活**的硬停走「新开一轮」⇒ **不得**发出带 `action: "resume"`
 *   的 `recovery` 请求（把 E3-B §3.2 的"分流"钉成行为，而不是只钉文案）。
 */
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../../App";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

/** 与 `api.ts` 里 `configuredBaseUrl` 同一条推导（不含末尾 `/`）。 */
const BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

const CONV = "conv-recovery-wiring";
const LANE = "lane-recovery-wiring-main";
const WORKSPACE = "ws-recovery-wiring";
const ESCALATED_RUN = "run-recovery-wiring-1";

const conversation = {
  id: CONV,
  title: "硬停后接着跑",
  status: "active",
  nextTurnOrdinal: 2,
  titleIsManual: false,
  createdAt: "2026-09-15T10:00:00Z",
  updatedAt: "2026-09-15T10:20:00Z",
  archivedAt: null,
  parentConversationId: null,
  forkTurnId: null,
  kind: "normal",
  providerProfileId: null,
  modelOverride: null,
  promotedAt: null,
  workspaceId: WORKSPACE,
};

const conversationSnapshot = { conversation, turns: [], files: [] };

const mainLane = {
  id: LANE,
  conversationId: CONV,
  kind: "main",
  status: "active",
  archived: false,
  archivedAt: null,
  displayName: null,
  summary: null,
  title: null,
  baseEntryExcerpt: null,
  baseEntryId: null,
  leafEntryId: null,
  createdFromEntryId: null,
  createdAt: "2026-09-01T00:00:00Z",
  sourceLaneId: null,
  isMain: true,
};

/**
 * 后端下发的**硬停**升级（`willStop=true`；终态后特意保留）。`revivableRunId`
 * 是后端判据："这条硬停 run 能不能原地复活"——前端**不猜**状态。
 */
const recoverySnapshot = (revivableRunId: string | null) => ({
  snapshotVersion: 1,
  conversationId: CONV,
  activeLaneId: LANE,
  mainLaneId: LANE,
  runningLaneId: null,
  runningRunId: null,
  activeRunId: ESCALATED_RUN,
  activeRunVariantId: ESCALATED_RUN,
  revivableRunId,
  lastEventSeq: 7,
  entries: [],
  runState: null,
  pendingApprovals: [],
  toolStates: [],
  contextUsage: { inputTokens: 0, outputTokens: 0 },
  interruptedRuns: [],
  stuck: null,
  escalation: {
    runId: ESCALATED_RUN,
    reason: "time_budget_exhausted",
    summary: "运行时长已到阈值（15 分钟）。",
    options: ["continue", "change_approach", "take_over"],
    progress: {
      modelTurns: 1,
      toolCalls: 1,
      toolFailures: 0,
      producedCharacters: 7,
      inputTokens: 11,
      outputTokens: 4,
      elapsedSeconds: 900,
    },
    budget: {
      usedTokens: 15,
      limitTokens: null,
      usedRatio: 0,
      usedSeconds: 900,
      limitSeconds: 900,
    },
    repeatedFailures: [],
    failures: [],
    guidance: "到上限不是故障。",
    willStop: true,
  },
  verification: null,
  usage: null,
  capabilities: [],
});

const provider = {
  id: "provider-recovery-wiring",
  name: "本地模型",
  kind: "openai_compatible",
  baseUrl: "http://127.0.0.1:9",
  defaultModel: "m1",
  timeoutSeconds: 60,
  enabled: true,
  isBuiltin: false,
  isDefault: true,
  configured: true,
  apiKeyConfigured: true,
  connectionState: "ready",
  lastCheckedAt: null,
  lastError: null,
  models: [],
  createdAt: "2026-09-01T00:00:00Z",
  updatedAt: "2026-09-01T00:00:00Z",
};

type CapturedRequest = {
  url: string;
  method: string;
  body: unknown;
};

let captured: CapturedRequest[];
let revivableRunId: string | null;
/** 未路由的 URL（响亮失败：出现即说明 App 打到了本文件没预料到的地方）。 */
let unrouted: string[];

const json = (payload: unknown, status = 200): Response =>
  new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });

/** SSE：空流（`body` 立即可读完），App 的读循环立刻 `done` 退出。 */
const emptyStream = (): Response =>
  new Response("", {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });

/**
 * 按 URL 路由回**真实 fetch**。记录发生在被测代码与网络之间，所以断言的对象
 * 就是"线上真正会发出去的请求"。
 */
const route = (rawUrl: string, init?: RequestInit): Response => {
  const method = (init?.method ?? "GET").toUpperCase();
  const body =
    typeof init?.body === "string" ? JSON.parse(init.body) : init?.body;
  captured.push({ url: rawUrl, method, body });

  const path = BASE && rawUrl.startsWith(BASE) ? rawUrl.slice(BASE.length) : rawUrl;
  const [routePath] = path.split("?");

  if (method === "GET" && routePath === "/health") return json({ status: "ok" });
  if (method === "GET" && routePath === "/capabilities")
    return json({ capabilities: [] });
  if (method === "GET" && routePath === "/workspaces")
    return json({
      items: [
        {
          id: WORKSPACE,
          name: "工作区",
          rootPath: "/tmp/ws-recovery-wiring",
          createdAt: "2026-09-01T00:00:00Z",
          updatedAt: "2026-09-01T00:00:00Z",
        },
      ],
      homePath: "/tmp",
    });
  if (method === "GET" && routePath === "/providers")
    return json({ items: [provider] });
  if (method === "GET" && routePath === "/conversations")
    return json({ items: [conversation] });
  if (method === "GET" && routePath === `/conversations/${CONV}`)
    return json(conversationSnapshot);
  if (method === "GET" && routePath === `/conversations/${CONV}/workspace`)
    return json({
      conversationId: CONV,
      visible: false,
      artifacts: [],
      pendingProposals: [],
    });
  if (method === "GET" && routePath === `/conversations/${CONV}/artifact-proposals`)
    return json({ items: [] });
  if (method === "GET" && routePath === `/conversations/${CONV}/memory-proposals`)
    return json({ items: [] });
  if (method === "GET" && routePath === `/conversations/${CONV}/task-proposals`)
    return json({ items: [] });
  if (method === "GET" && routePath === `/conversations/${CONV}/knowledge-proposals`)
    return json({ items: [] });
  if (method === "GET" && routePath === `/conversations/${CONV}/turns`)
    return json({ items: [] });
  if (method === "GET" && routePath === `/conversations/${CONV}/citations`)
    return json({ turnCitations: {} });
  if (method === "GET" && routePath === `/conversations/${CONV}/undo-journal`)
    return json({ items: [], latestAvailableId: null });
  if (method === "GET" && routePath === "/skills/invocable")
    return json({ items: [] });
  if (method === "GET" && routePath === "/notifications")
    return json({ items: [] });
  if (method === "GET" && routePath === "/proposals/pending")
    return json({ items: [] });
  if (method === "GET" && routePath === "/tasks") return json({ items: [] });
  if (method === "GET" && routePath === "/reminders") return json({ items: [] });
  if (
    method === "GET" &&
    routePath === `/api/v2/conversations/${CONV}/lanes`
  )
    return json({
      conversationId: CONV,
      activeLaneId: LANE,
      items: [mainLane],
    });
  if (
    method === "GET" &&
    routePath === `/api/v2/conversations/${CONV}/snapshot`
  )
    return json(recoverySnapshot(revivableRunId));
  if (
    method === "GET" &&
    routePath === `/api/v2/conversations/${CONV}/events`
  )
    return emptyStream();
  if (method === "GET" && routePath === "/api/v2/hub/events")
    return emptyStream();
  // ★ 本文件存在的理由：这条恢复请求的**请求体**。
  if (method === "POST" && routePath === `/api/v2/runs/${ESCALATED_RUN}/recovery`)
    return json({
      runId: ESCALATED_RUN,
      action: "resumed",
      laneId: LANE,
      newRunId: null,
    });
  if (
    method === "POST" &&
    routePath === `/api/v2/conversations/${CONV}/references/preview`
  )
    return json({ conversationId: CONV, totalTokenEstimate: 0, warnings: [], items: [] });

  unrouted.push(`${method} ${path}`);
  return json(
    { error: { code: "not_found", message: `测试未路由：${method} ${path}` } },
    404,
  );
};

/** 所有打到恢复端点的请求（反向断言用："零恢复请求" ⊃ "零 resume 请求"）。 */
const recoveryCalls = () =>
  captured.filter(
    (item) =>
      item.method === "POST" &&
      item.url.endsWith(`/api/v2/runs/${ESCALATED_RUN}/recovery`),
  );

/** 其中带 `action: "resume"` 的那批——不可复活时必须是空集。 */
const resumeRecoveryCalls = () =>
  recoveryCalls().filter(
    (item) => (item.body as { action?: string } | undefined)?.action === "resume",
  );

let container: HTMLDivElement;
let root: Root;

const flush = (times = 12) =>
  act(async () => {
    for (let i = 0; i < times; i += 1) await Promise.resolve();
  });

const button = (label: string) =>
  Array.from(container.querySelectorAll("button")).find(
    (item) => item.textContent === label,
  );

const renderApp = async () => {
  await act(async () => {
    root.render(<App />);
  });
  await flush(30);
  await flush(30);
};

beforeEach(() => {
  // jsdom 没有布局：SessionRail 的 `scrollIntoView` 不存在，会被 rAF 回调打到。
  (Element.prototype as unknown as { scrollIntoView: () => void }).scrollIntoView =
    vi.fn();
  captured = [];
  unrouted = [];
  revivableRunId = ESCALATED_RUN;
  vi.stubGlobal(
    "fetch",
    (input: unknown, init?: RequestInit): Promise<Response> => {
      try {
        return Promise.resolve(route(String(input), init));
      } catch (error) {
        return Promise.reject(error);
      }
    },
  );
  localStorage.clear();
  localStorage.setItem(
    "endless-task.active-conversation",
    JSON.stringify({ conversationId: CONV, workspaceId: WORKSPACE }),
  );
  localStorage.setItem("endless-task.workspace", WORKSPACE);
  window.location.hash = "";
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("E3/B I-1：真 App 里的恢复动作接线（useMessageActions → api.ts → 请求体）", () => {
  it("可复活：点「继续（接上这条运行）」⇒ 实际发出 POST /api/v2/runs/{runId}/recovery，请求体 action === \"resume\"", async () => {
    await renderApp();

    const resume = button("继续（接上这条运行）");
    expect(resume).not.toBeUndefined();

    await act(async () => resume!.click());
    await flush(30);
    await flush(30);

    // 写死成 `"retry"` 的那两处（`useMessageActions.ts` / `api.ts` 请求体）都会在这里红：
    // 前者让 URL 之外的实参变形，后者让请求体变形——两者都逃不过这条断言。
    const calls = recoveryCalls();
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(
      `${BASE}/api/v2/runs/${ESCALATED_RUN}/recovery`,
    );
    expect(calls[0].body).toEqual({ action: "resume" });

    // 覆盖面的自检：这次真 App 挂载 + 点击打到**每一个**端点都必须有路由。
    // 有任何一个落到兜底 404，说明用例的 stub 面有洞（可能悄悄改变了行为）。
    expect(unrouted).toEqual([]);
  });

  it("不可复活：点「新开一轮」⇒ 不得发出带 action: \"resume\" 的 recovery 请求", async () => {
    revivableRunId = null;
    await renderApp();

    // 分流的行为面：不可复活时**没有**复活按钮，只有「新开一轮」。
    expect(button("继续（接上这条运行）")).toBeUndefined();
    const freshTurn = button("新开一轮");
    expect(freshTurn).not.toBeUndefined();

    await act(async () => freshTurn!.click());
    await flush(30);
    await flush(30);

    // 反向断言：不许借用复活通道（连恢复端点都不该被打到）。
    expect(resumeRecoveryCalls()).toHaveLength(0);
    expect(recoveryCalls()).toHaveLength(0);
    // 同上一节：不可复活这条路也不许有未路由的端点。
    expect(unrouted).toEqual([]);
  });
});
