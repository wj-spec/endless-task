import { expect, type APIRequestContext, type Page } from "@playwright/test";
import { mkdirSync } from "node:fs";

export const apiUrl = `http://127.0.0.1:${process.env.ENDLESS_TASK_E2E_API_PORT ?? "18000"}`;

let tmpCounter = 0;
const tempDir = () => {
  const path = `${process.env.ENDLESS_TASK_E2E_TMPDIR ?? "/tmp"}/endless-e2e-${Date.now()}-${tmpCounter++}`;
  mkdirSync(path, { recursive: true });
  return path;
};

type Conversation = {
  id: string;
  title: string;
  kind: string;
};

type Workspace = {
  id: string;
  name: string;
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
  const workspace = await createWorkspace(request, `工作区 ${title}`);
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
  return { conversation, handle };
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
  options: { mobile?: boolean } = {},
) => {
  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();
  if (options.mobile) {
    await page.getByRole("button", { name: "展开侧栏" }).click();
  }
  const navigation = page.getByRole("navigation", { name: "会话列表" });
  // 会话归属已绑定工作区，且工作区组默认收起；展开所有收起的工作区组，
  // 直到没有收起项，再定位目标会话行。
  const row = navigation.locator(".session-item-main").filter({ hasText: title });
  for (let guard = 0; guard < 50; guard += 1) {
    const collapsed = navigation.locator('.workspace-item-main[aria-expanded="false"]');
    const count = await collapsed.count();
    if (count === 0) break;
    await collapsed.first().click();
  }
  await row.click();
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