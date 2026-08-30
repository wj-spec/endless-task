import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const apiUrl = `http://127.0.0.1:${process.env.ENDLESS_TASK_E2E_API_PORT ?? "18000"}`;

type Conversation = {
  id: string;
  title: string;
  kind: string;
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

export const createConversation = async (
  request: APIRequestContext,
  title: string,
): Promise<Conversation> => {
  const created = await json<Conversation>(
    await request.post(`${apiUrl}/conversations`, { data: {} }),
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

export const openConversation = async (
  page: Page,
  title: string,
  options: { mobile?: boolean } = {},
) => {
  await page.goto("/");
  await expect(page.getByText("服务已连接").first()).toBeVisible();
  if (options.mobile) {
    await page.getByRole("button", { name: "展开侧栏" }).click();
  }
  const navigation = page.getByRole("navigation", { name: "会话列表" });
  await navigation.locator(".session-item-main").filter({ hasText: title }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
};