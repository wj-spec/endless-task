import { expect, test } from "@playwright/test";
import {
  apiUrl,
  createCompletedWorkspaceConversation,
  createWorkspace,
  openConversation,
} from "../support/api";

/**
 * 回归：会话的「摘要式标题」与侧栏同步。
 *
 * 两处都曾缺失：
 * 1. v2 消息路径不做自动命名（v1 的 `create_turn` 才有），会话永远叫「新对话」；
 * 2. 侧栏 `SessionRail` 用独立数据源（`useWorkspaceNavigation`），新建会话 /
 *    标题变化不会触发它刷新 —— 新建的会话根本不出现，标题也停在「新对话」。
 */
test("新建会话出现在侧栏，首条消息后变成摘要式标题", async ({
  page,
  request,
}) => {
  const workspace = await createWorkspace(request, `命名工作区 ${Date.now()}`);
  // 先在工作区里放一段对话，进入页面时该工作区即为当前工作区（「+」只出现在当前行）。
  const entryTitle = `命名入口 ${Date.now()}`;
  await createCompletedWorkspaceConversation(request, workspace.id, entryTitle);
  await openConversation(page, entryTitle, { workspaceId: workspace.id });

  const nav = page.getByRole("navigation", { name: "会话列表" });
  const group = nav.locator(`[data-workspace-id="${workspace.id}"]`);
  const sessions = group.locator(".session-item-main");

  // 工作区行的「+」新建会话 → 侧栏**立即**出现一行（不需要刷新页面）
  await group.getByRole("button", { name: "在当前工作区新建会话" }).click();
  await expect(sessions.filter({ hasText: "新对话" })).toBeVisible();

  // 首条消息 → 标题变为前 30 字（与后端 v1 同一规则），侧栏行同步更新
  const message = `验证摘要标题 ${Date.now()} 这条消息应当成为侧栏里的会话标题`;
  const textarea = page.getByLabel("给 Endless 发送消息").first();
  await textarea.fill(message);
  await page.getByRole("button", { name: "发送消息" }).first().click();

  const expected = message.slice(0, 30);
  await expect(sessions.filter({ hasText: expected })).toBeVisible();
  await expect(
    page.locator(".heading-sub").filter({ hasText: expected }),
  ).toBeVisible();

  // 后端也真的改了标题（不只是前端乐观更新）
  const conversations = await request
    .get(`${apiUrl}/conversations?status=active&workspace=${workspace.id}`)
    .then((response) => response.json());
  expect(
    conversations.items.some((item: { title: string }) => item.title === expected),
  ).toBe(true);
});

test("手动命名的标题不会被首条消息覆盖", async ({ page, request }) => {
  const workspace = await createWorkspace(request, `手动命名 ${Date.now()}`);
  const entryTitle = `手动入口 ${Date.now()}`;
  await createCompletedWorkspaceConversation(request, workspace.id, entryTitle);
  await openConversation(page, entryTitle, { workspaceId: workspace.id });

  const nav = page.getByRole("navigation", { name: "会话列表" });
  const group = nav.locator(`[data-workspace-id="${workspace.id}"]`);
  await group.getByRole("button", { name: "在当前工作区新建会话" }).click();
  // 等新会话真正成为当前会话（否则重命名/发消息会落在上一段对话上）
  await expect(page.locator(".heading-sub")).toHaveText("新对话");

  const row = group.locator(".session-item").filter({ hasText: "新对话" }).first();
  await expect(row).toBeVisible();
  await row.getByRole("button", { name: /管理对话：/ }).click();
  await page.getByRole("menuitem", { name: "重命名" }).click();
  const input = group.getByLabel("对话名称");
  await input.fill("我自己起的名字");
  await input.press("Enter");
  await expect(
    group.locator(".session-item-main").filter({ hasText: "我自己起的名字" }),
  ).toBeVisible();

  const textarea = page.getByLabel("给 Endless 发送消息").first();
  await textarea.fill("这条消息不应该覆盖手动标题");
  await page.getByRole("button", { name: "发送消息" }).first().click();
  await expect(
    page.locator(".heading-sub").filter({ hasText: "我自己起的名字" }),
  ).toBeVisible();
});
