import { expect, test } from "@playwright/test";
import {
  apiUrl,
  createCompletedConversation,
  createTemporaryConversationFromMenu,
  openConversation,
  seedProposal,
  waitForRunStatus,
} from "../support/api";

test("四类 Proposal 接受后可打开对应结果位置", async ({ page, request }) => {
  const title = `E2E Proposal Results ${Date.now()}`;
  const { conversation, handle } = await createCompletedConversation(request, title);
  for (const kind of ["artifact", "knowledge", "memory", "task"] as const) {
    await seedProposal(request, conversation.id, handle.runId, kind);
  }
  await openConversation(page, title);

  const artifactCard = page.locator("article.proposal-card").filter({
    hasText: "E2E 发布记录",
  });
  await artifactCard.getByRole("button", { name: "保留为 Artifact" }).click();
  await expect(artifactCard.getByText(/已保存到工作区 Artifact/)).toBeVisible();
  await artifactCard.getByRole("button", { name: "打开 Artifact" }).click();
  const workspace = page.locator("aside.workspace-panel");
  await expect(workspace).toBeVisible();
  await expect(workspace.getByText("E2E 发布记录")).toBeVisible();
  await workspace.getByRole("button", { name: "收起" }).click();

  const knowledgeCard = page.locator("article.proposal-card").filter({
    hasText: "E2E 知识条目",
  });
  await knowledgeCard.getByRole("button", { name: "加入知识" }).click();
  await expect(page.getByText("已加入全局知识")).toBeVisible();
  await page.getByRole("button", { name: "打开知识" }).click();
  const knowledgePanel = page.getByRole("complementary", {
    name: "知识与记忆面板",
  });
  await expect(knowledgePanel.getByRole("tab", { name: "知识" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(knowledgePanel.getByText("E2E 知识条目")).toBeVisible();
  await knowledgePanel
    .getByRole("button", { name: "关闭知识与记忆面板" })
    .click();

  const memoryCard = page.locator("article.proposal-card").filter({
    hasText: "E2E 用户偏好简洁回答",
  });
  await memoryCard.getByRole("button", { name: "记住" }).click();
  await expect(page.getByText("已保存到记忆")).toBeVisible();
  await page.getByRole("button", { name: "打开记忆" }).click();
  const memoryPanel = page.getByRole("complementary", {
    name: "知识与记忆面板",
  });
  await expect(memoryPanel.getByRole("tab", { name: "记忆" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(memoryPanel.getByText(/E2E 用户偏好简洁回答/)).toBeVisible();
  await memoryPanel
    .getByRole("button", { name: "关闭知识与记忆面板" })
    .click();

  const taskCard = page.locator("article.proposal-card").filter({
    hasText: "每周检查发布状态",
  });
  await taskCard.getByRole("button", { name: "安排上" }).click();
  await expect(page.getByText(/已保存到已安排/)).toBeVisible();
  await page.getByRole("button", { name: "打开已安排" }).click();
  const taskPanel = page.getByRole("complementary", {
    name: "任务与通知面板",
  });
  await expect(taskPanel.getByRole("tab", { name: "已安排" })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(taskPanel.getByText(/E2E 每周发布检查/)).toBeVisible();
});

test("右侧 Temporary Conversation 接受 Artifact 后聚焦但保持临时属性", async ({
  page,
  request,
}) => {
  const title = `E2E Temporary Artifact ${Date.now()}`;
  const { conversation } = await createCompletedConversation(request, title);
  await openConversation(page, title);

  const createResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(
        `/api/v2/conversations/${conversation.id}/temporary-conversations`,
      ),
  );
  await createTemporaryConversationFromMenu(page);
  const temporary = (await (await createResponsePromise).json()) as {
    conversation: { id: string };
    lane: { id: string };
  };
  const side = page.getByRole("region", { name: "临时对话" });
  const messageResponsePromise = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().endsWith(
        `/api/v2/conversations/${temporary.conversation.id}/messages`,
      ),
  );
  await side
    .getByRole("textbox", { name: "给 Endless 发送消息" })
    .fill("创建临时 Artifact 提案");
  await side.getByRole("button", { name: "发送消息" }).click();
  const handle = (await (await messageResponsePromise).json()) as { runId: string };
  await seedProposal(request, temporary.conversation.id, handle.runId, "artifact");
  await waitForRunStatus(
    request,
    temporary.conversation.id,
    "completed",
    temporary.lane.id,
  );

  const artifactCard = side.locator("article.proposal-card").filter({
    hasText: "E2E 发布记录",
  });
  await expect(artifactCard).toBeVisible({ timeout: 10_000 });
  await artifactCard.getByRole("button", { name: "保留为 Artifact" }).click();
  await expect(artifactCard.getByText(/已保存到工作区 Artifact/)).toBeVisible();
  await artifactCard.getByRole("button", { name: "打开 Artifact" }).click();

  await expect(side).toBeHidden();
  await expect(page.getByText(/临时会话.*不写入记忆/)).toBeVisible();
  const workspace = page.locator("aside.workspace-panel");
  await expect(workspace).toBeVisible();
  await expect(workspace.getByText("E2E 发布记录")).toBeVisible();

  const temporaryResponse = await request.get(
    `${apiUrl}/conversations/${temporary.conversation.id}`,
  );
  expect(temporaryResponse.ok()).toBeTruthy();
  expect((await temporaryResponse.json()).conversation.kind).toBe("ephemeral");
});