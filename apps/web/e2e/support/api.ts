import { expect, type APIRequestContext, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";

export const apiUrl = `http://127.0.0.1:${process.env.ENDLESS_TASK_E2E_API_PORT ?? "18000"}`;

let tmpCounter = 0;
const tempDir = () => {
  const path = `${process.env.ENDLESS_TASK_E2E_TMPDIR ?? "/tmp"}/endless-e2e-${Date.now()}-${crypto.randomUUID().slice(0, 8)}-${tmpCounter++}`;
  mkdirSync(path, { recursive: true });
  return path;
};

type Conversation = {
  id: string;
  title: string;
  kind: string;
  workspace_id?: string | null;
};

type Workspace = {
  id: string;
  name: string;
  rootPath: string | null;
};

type Lane = {
  id: string;
  isMain: boolean;
  displayName: string | null;
};

type LaneList = {
  items: Lane[];
  mainLaneId: string | null;
};

type MessageHandle = {
  runId: string;
  laneId: string;
};

type ProposalKind = "artifact" | "knowledge" | "memory" | "task";

type RuntimeSnapshot = {
  runState: { status: string; partialContent: string } | null;
  entries: Array<{
    id: string;
    type: string;
    actor: string;
    data: { content?: string };
  }>;
};

const json = async <T>(response: Awaited<ReturnType<APIRequestContext["get"]>>) => {
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()) as T;
};

export const createWorkspace = async (
  request: APIRequestContext,
  name: string,
): Promise<Workspace> => {
  const created = await json<{ workspace: Workspace }>(
    await request.post(`${apiUrl}/workspaces`, {
      data: { name, rootPath: tempDir() },
    }),
  );
  return created.workspace;
};

export const bindWorkspaceRoot = async (
  request: APIRequestContext,
  workspaceId: string,
): Promise<Workspace> => {
  const patched = await json<{ workspace: Workspace }>(
    await request.patch(`${apiUrl}/workspaces/${workspaceId}`, {
      data: { rootPath: tempDir() },
    }),
  );
  return patched.workspace;
};

// 会话必须归属到已绑定目录的工作区（必选绑定设定）。测试统一用本 helper 建绑定工作区+会话。
export const createConversation = async (
  request: APIRequestContext,
  title: string,
): Promise<Conversation> => {
  // 会话必须归属到已绑定工作区；工作区名用短随机名（跨项目/进程唯一），
  // 避免标题超长触发名称长度上限，也避免多 worker 共享同一 DB 时名称冲突。
  const workspace = await createWorkspace(
    request,
    `会话工作区 ${crypto.randomUUID().slice(0, 8)}`,
  );
  return createWorkspaceConversation(request, workspace.id, title);
};

export const createWorkspaceConversation = async (
  request: APIRequestContext,
  workspaceId: string,
  title: string,
): Promise<Conversation> => {
  const created = await json<Conversation>(
    await request.post(`${apiUrl}/conversations`, {
      data: { workspaceId },
    }),
  );
  return json<Conversation>(
    await request.patch(`${apiUrl}/conversations/${created.id}`, {
      data: { title },
    }),
  );
};

export const sendMessage = async (
  request: APIRequestContext,
  conversationId: string,
  content: string,
  laneId?: string,
): Promise<MessageHandle> =>
  json<MessageHandle>(
    await request.post(
      `${apiUrl}/api/v2/conversations/${conversationId}/messages`,
      {
        data: { content, laneId: laneId ?? null },
        headers: { "Idempotency-Key": crypto.randomUUID() },
      },
    ),
  );

export const runtimeSnapshot = (
  request: APIRequestContext,
  conversationId: string,
  laneId?: string,
): Promise<RuntimeSnapshot> => {
  const suffix = laneId ? `?lane_id=${encodeURIComponent(laneId)}` : "";
  return request
    .get(`${apiUrl}/api/v2/conversations/${conversationId}/snapshot${suffix}`)
    .then(json<RuntimeSnapshot>);
};

export const waitForRunStatus = async (
  request: APIRequestContext,
  conversationId: string,
  status: string,
  laneId?: string,
) => {
  await expect
    .poll(
      async () => (await runtimeSnapshot(request, conversationId, laneId)).runState?.status,
      { timeout: 10_000 },
    )
    .toBe(status);
};

export const seedProposal = async (
  request: APIRequestContext,
  conversationId: string,
  turnId: string,
  kind: ProposalKind,
): Promise<string> => {
  const result = await json<{ id: string }>(
    await request.post(`${apiUrl}/__e2e/proposals`, {
      data: { conversationId, turnId, kind },
    }),
  );
  return result.id;
};

export const createCompletedConversation = async (
  request: APIRequestContext,
  title: string,
  content = "E2E 基线消息",
) => {
  const conversation = await createConversation(request, title);
  const handle = await sendMessage(request, conversation.id, content);
  await waitForRunStatus(request, conversation.id, "completed", handle.laneId);
  // workspaceId 供 openConversation 精确定位工作区组（避免全量展开）。
  return { conversation, handle, workspaceId: conversation.workspace_id ?? null };
};

export const createCompletedWorkspaceConversation = async (
  request: APIRequestContext,
  workspaceId: string,
  title: string,
  content = "E2E 工作区基线消息",
) => {
  const conversation = await createWorkspaceConversation(
    request,
    workspaceId,
    title,
  );
  const handle = await sendMessage(request, conversation.id, content);
  await waitForRunStatus(request, conversation.id, "completed", handle.laneId);
  return { conversation, handle };
};

export const listLanes = (request: APIRequestContext, conversationId: string) =>
  request
    .get(`${apiUrl}/api/v2/conversations/${conversationId}/lanes`)
    .then(json<LaneList>);

export const createBranch = async (
  request: APIRequestContext,
  conversationId: string,
  sourceLaneId: string,
  displayName: string,
): Promise<Lane> => {
  const response = await json<{ lane: Lane }>(
    await request.post(`${apiUrl}/api/v2/conversations/${conversationId}/lanes`, {
      data: {
        kind: "persistent_branch",
        sourceLaneId,
        displayName,
      },
    }),
  );
  return response.lane;
};

export const createTemporaryConversationFromMenu = async (page: Page) => {
  await page.getByRole("button", { name: "更多会话操作" }).click();
  await page.getByRole("menuitem", { name: "从主线创建临时对话" }).click();
};

export const openConversation = async (
  page: Page,
  title: string,
  options: { mobile?: boolean; workspaceId?: string | null } = {},
) => {
  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();
  if (options.mobile) {
    await page.getByRole("button", { name: "展开侧栏" }).click();
  }
  const navigation = page.getByRole("navigation", { name: "会话列表" });
  const row = navigation.locator(".session-item-main").filter({ hasText: title });

  if (options.workspaceId) {
    // 已知归属：只展开目标工作区组（全量共享 DB 里可能有 60+ 个组，
    // 逐个点开会吃掉测试超时预算）。
    const toggle = navigation
      .locator(`[data-workspace-id="${options.workspaceId}"]`)
      .locator(".workspace-item-main");
    await expect(toggle).toBeVisible();
    if ((await toggle.getAttribute("aria-expanded")) === "false") {
      await toggle.click();
    }
  } else {
    // 未知归属：逐个展开收起的工作区组，**一找到目标行就停**（不再是有上限的
    // 全量展开——上限一到就静默放弃，表现为后面 click 干等到超时）。
    for (let guard = 0; guard < 200; guard += 1) {
      if (await row.count()) break;
      const collapsed = navigation.locator(
        '.workspace-item-main[aria-expanded="false"]',
      );
      if ((await collapsed.count()) === 0) break;
      await collapsed.first().click();
    }
  }

  await row.first().click();
  // 会话归属已绑定工作区时，标题渲染在 .heading-sub；否则为 .heading-title。
  await expectConversationHeading(page, title);
};

// 会话标题断言：绑定工作区时在 .heading-sub，未绑定时在 .heading-title。
export const expectConversationHeading = async (
  page: Page,
  title: string,
) => {
  await expect(
    page.locator(".heading-sub, .heading-title").filter({ hasText: title }).first(),
  ).toBeVisible();
};