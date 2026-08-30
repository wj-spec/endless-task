import { expect, test } from "@playwright/test";
import {
  apiUrl,
  createBranch,
  createCompletedConversation,
  listLanes,
  openConversation,
  runtimeSnapshot,
  waitForRunStatus,
} from "../support/api";

test("Branch 可右侧继续和聚焦，只有显式操作才改变主线", async ({
  page,
  request,
}) => {
  const title = `E2E Branch ${Date.now()}`;
  const { conversation } = await createCompletedConversation(request, title);
  const initialLanes = await listLanes(request, conversation.id);
  const originalMainId = initialLanes.mainLaneId!;
  const branch = await createBranch(
    request,
    conversation.id,
    originalMainId,
    "验证分支",
  );
  await openConversation(page, title);

  await page.getByRole("button", { name: /^主线/ }).click();
  await page.getByRole("button", { name: "管理分支：验证分支" }).click();
  await page.getByRole("menuitem", { name: "右侧对照" }).click();

  const side = page.getByRole("region", { name: "分支对照" });
  await expect(side).toBeVisible();
  await expect(side.getByText("关闭仅收起，不删除分支")).toBeVisible();
  await side.getByRole("textbox", { name: "给 Endless 发送消息" }).fill("分支独立消息");
  await side.getByRole("button", { name: "发送消息" }).click();
  await expect(side.getByText("分支独立消息")).toBeVisible();
  await expect(side.getByText("E2E 确定性回复").last()).toBeVisible();
  await expect(side.getByRole("button", { name: "发送消息" })).toBeVisible();

  expect((await listLanes(request, conversation.id)).mainLaneId).toBe(originalMainId);
  await side.getByRole("button", { name: "收起分支对照" }).click();
  await expect(side).toBeHidden();
  expect((await listLanes(request, conversation.id)).items.some((lane) => lane.id === branch.id)).toBe(
    true,
  );

  await page.getByRole("button", { name: /^主线/ }).click();
  await page
    .locator(".branch-navigator-select")
    .filter({ hasText: "验证分支" })
    .click();
  await expect(page.getByText(/正在查看分支「验证分支」/)).toBeVisible();
  expect((await listLanes(request, conversation.id)).mainLaneId).toBe(originalMainId);

  const regenerateResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      /\/api\/v2\/runs\/[^/]+\/regenerate$/.test(response.url()),
  );
  await page.getByRole("button", { name: "重新生成" }).click();
  expect((await regenerateResponsePromise).ok()).toBeTruthy();
  await waitForRunStatus(request, conversation.id, "completed", branch.id);
  await expect(page.getByRole("main").getByText("分支独立消息")).toBeVisible();
  await expect(page.getByText(/正在查看分支「验证分支」/)).toBeVisible();
  expect((await listLanes(request, conversation.id)).mainLaneId).toBe(originalMainId);

  await page.getByRole("button", { name: "设为主线" }).click();
  await expect.poll(async () => (await listLanes(request, conversation.id)).mainLaneId).toBe(
    branch.id,
  );
  const promotedLanes = await listLanes(request, conversation.id);
  expect(promotedLanes.items.some((lane) => lane.id === originalMainId && !lane.isMain)).toBe(
    true,
  );
});

test("同一 Conversation 跨 Lane 冲突隔离，关闭运行中分支会取消 Run", async ({
  page,
  request,
}) => {
  const title = `E2E Lane Conflict ${Date.now()}`;
  const { conversation } = await createCompletedConversation(request, title);
  const initialLanes = await listLanes(request, conversation.id);
  const mainLaneId = initialLanes.mainLaneId!;
  const branch = await createBranch(
    request,
    conversation.id,
    mainLaneId,
    "运行中分支",
  );
  await openConversation(page, title);

  await page.getByRole("button", { name: /^主线/ }).click();
  await page.getByRole("button", { name: "管理分支：运行中分支" }).click();
  await page.getByRole("menuitem", { name: "右侧对照" }).click();

  const main = page.getByRole("main");
  const side = page.getByRole("region", { name: "分支对照" });
  await side
    .getByRole("textbox", { name: "给 Endless 发送消息" })
    .fill("[e2e:pause] 保持分支运行");
  await side.getByRole("button", { name: "发送消息" }).click();
  await expect
    .poll(async () => {
      const response = await request.get(`${apiUrl}/__e2e/provider`);
      return (await response.json()).pausedCount as number;
    })
    .toBe(1);

  await expect(main.getByText(/运行中分支.*正在运行/)).toBeVisible();
  await expect(main.getByRole("button", { name: "查看运行位置" })).toBeVisible();
  await expect(main.getByRole("button", { name: "停止后继续" })).toBeVisible();
  await expect(main.getByRole("textbox", { name: "给 Endless 发送消息" })).toBeDisabled();
  await expect(main.getByRole("button", { name: "停止生成" })).toHaveCount(0);
  await expect(side.getByRole("button", { name: "停止生成" })).toBeVisible();

  const conflict = await request.post(
    `${apiUrl}/api/v2/conversations/${conversation.id}/messages`,
    {
      data: { content: "不应创建第二个 Run", laneId: mainLaneId },
      headers: { "Idempotency-Key": crypto.randomUUID() },
    },
  );
  expect(conflict.status()).toBe(409);

  await page.keyboard.press("Escape");
  const dialog = page.getByRole("dialog", { name: "收起运行中的分支" });
  await expect(dialog.getByText(/停止当前运行并收起右侧工作面/)).toBeVisible();
  await dialog.getByRole("button", { name: "停止并收起" }).click();

  await expect(side).toBeHidden();
  await expect(main.getByRole("textbox", { name: "给 Endless 发送消息" })).toBeEnabled();
  await expect
    .poll(
      async () =>
        (await runtimeSnapshot(request, conversation.id, branch.id)).runState?.status,
    )
    .toBe("cancelled");
  expect(
    (await listLanes(request, conversation.id)).items.some(
      (lane) => lane.id === branch.id,
    ),
  ).toBe(true);
});

test("刷新后仍跟踪其他 Lane 的活动 Run", async ({ page, request }) => {
  const title = `E2E Cross Lane Refresh ${Date.now()}`;
  const { conversation } = await createCompletedConversation(request, title);
  const initialLanes = await listLanes(request, conversation.id);
  const branch = await createBranch(
    request,
    conversation.id,
    initialLanes.mainLaneId!,
    "后台运行分支",
  );
  const started = await request.post(
    `${apiUrl}/api/v2/conversations/${conversation.id}/messages`,
    {
      data: { content: "[e2e:pause] 刷新后继续跟踪", laneId: branch.id },
      headers: { "Idempotency-Key": crypto.randomUUID() },
    },
  );
  expect(started.ok()).toBeTruthy();
  await waitForRunStatus(request, conversation.id, "running", branch.id);

  await openConversation(page, title);
  const main = page.getByRole("main");
  await expect(main.getByText(/后台运行分支.*正在运行/)).toBeVisible();
  await page.reload();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
  await expect(main.getByText(/后台运行分支.*正在运行/)).toBeVisible();
  await expect(main.getByRole("textbox", { name: "给 Endless 发送消息" })).toBeDisabled();

  const resume = await request.post(`${apiUrl}/__e2e/provider/resume`);
  expect(resume.ok()).toBeTruthy();
  expect((await resume.json()).resumedCount).toBe(1);
  await expect(main.getByText(/后台运行分支.*正在运行/)).toBeHidden();
  await expect(main.getByRole("textbox", { name: "给 Endless 发送消息" })).toBeEnabled();
});

test("Temporary Conversation 可与来源 Conversation 并行且状态互不污染", async ({
  page,
  request,
}) => {
  const title = `E2E Temporary Parallel ${Date.now()}`;
  const { conversation } = await createCompletedConversation(request, title);
  await openConversation(page, title);

  const createResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`/api/v2/conversations/${conversation.id}/temporary-conversations`),
  );
  await page.getByRole("button", { name: "开临时会话" }).click();
  const temporaryPayload = (await (await createResponsePromise).json()) as {
    conversation: { id: string };
    lane: { id: string };
  };

  const main = page.getByRole("main");
  const side = page.getByRole("region", { name: "临时对话" });
  await main
    .getByRole("textbox", { name: "给 Endless 发送消息" })
    .fill("[e2e:pause] 来源会话继续运行");
  await main.getByRole("button", { name: "发送消息" }).click();
  await expect
    .poll(async () => {
      const response = await request.get(`${apiUrl}/__e2e/provider`);
      return (await response.json()).pausedCount as number;
    })
    .toBe(1);
  await expect(main.getByRole("button", { name: "停止生成" })).toBeVisible();

  await side
    .getByRole("textbox", { name: "给 Endless 发送消息" })
    .fill("临时会话并行消息");
  await side.getByRole("button", { name: "发送消息" }).click();
  await waitForRunStatus(
    request,
    temporaryPayload.conversation.id,
    "completed",
    temporaryPayload.lane.id,
  );
  await expect(side.getByText("临时会话并行消息")).toBeVisible();
  await expect(side.getByRole("button", { name: "发送消息" })).toBeVisible();
  expect((await runtimeSnapshot(request, conversation.id)).runState?.status).toBe("running");
  await expect(main.getByRole("button", { name: "停止生成" })).toBeVisible();

  const resume = await request.post(`${apiUrl}/__e2e/provider/resume`);
  expect(resume.ok()).toBeTruthy();
  expect((await resume.json()).resumedCount).toBe(1);
  await expect(main.getByText("E2E 确定性回复，恢复后完成")).toBeVisible();
});

test("Temporary Conversation 隔离写入，可丢弃或升级", async ({ page, request }) => {
  const title = `E2E Temporary ${Date.now()}`;
  const { conversation } = await createCompletedConversation(request, title);
  await openConversation(page, title);

  const createResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`/api/v2/conversations/${conversation.id}/temporary-conversations`),
  );
  await page.getByRole("button", { name: "开临时会话" }).click();
  const createResponse = await createResponsePromise;
  expect(createResponse.ok()).toBeTruthy();
  const temporaryPayload = (await createResponse.json()) as {
    conversation: { id: string };
    lane: { id: string };
  };
  const temporaryId = temporaryPayload.conversation.id;
  const temporaryLaneId = temporaryPayload.lane.id;

  const side = page.getByRole("region", { name: "临时对话" });
  await expect(side.getByText("关闭即删除")).toBeVisible();
  await side.getByRole("textbox", { name: "给 Endless 发送消息" }).fill("临时隔离消息");
  await side.getByRole("button", { name: "发送消息" }).click();
  await expect(side.getByText("临时隔离消息")).toBeVisible();
  await waitForRunStatus(request, temporaryId, "completed", temporaryLaneId);
  await expect(side.getByRole("button", { name: "发送消息" })).toBeVisible();

  const sourceSnapshot = await runtimeSnapshot(request, conversation.id);
  expect(
    sourceSnapshot.entries.some((entry) => entry.data.content === "临时隔离消息"),
  ).toBe(false);

  await page.keyboard.press("Escape");
  const dialog = page.getByRole("dialog", { name: "关闭临时对话" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole("button", { name: "取消" })).toBeFocused();
  const deleteResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "DELETE" &&
      response.url().endsWith(`/api/v2/temporary-conversations/${temporaryId}`),
  );
  await dialog.getByRole("button", { name: "删除临时对话" }).click();
  expect((await deleteResponsePromise).ok()).toBeTruthy();
  await expect(side).toBeHidden();
  expect((await request.get(`${apiUrl}/conversations/${temporaryId}`)).status()).toBe(404);

  const promoteCreatePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(`/api/v2/conversations/${conversation.id}/temporary-conversations`),
  );
  await page.getByRole("button", { name: "开临时会话" }).click();
  const promotedConversationId = (await (await promoteCreatePromise).json()).conversation.id as string;
  const promoteResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(
        `/api/v2/temporary-conversations/${promotedConversationId}/promote`,
      ),
  );
  await page
    .getByRole("region", { name: "临时对话" })
    .getByRole("button", { name: "升级为正式" })
    .click();
  expect((await promoteResponsePromise).ok()).toBeTruthy();
  await expect(page.getByRole("region", { name: "临时对话" })).toBeHidden();

  const promoted = await request.get(`${apiUrl}/conversations/${promotedConversationId}`);
  expect(promoted.ok()).toBeTruthy();
  expect((await promoted.json()).conversation.kind).not.toBe("ephemeral");
});