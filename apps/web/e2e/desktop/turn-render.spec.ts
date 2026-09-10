import { expect, test } from "@playwright/test";
import {
  createConversation,
  expectConversationHeading,
  openConversation,
  sendMessage,
  waitForRunStatus,
} from "../support/api";

/**
 * 轮次渲染的浏览器级冒烟（"drill"）。
 *
 * 为什么单列一条：拆 `ChatWorkSurface` 的轮次渲染块时踩过一次坑——661 行 JSX 一次性
 * 搬迁后 `tsc -b` 通过、330 条 jsdom 单测全绿，但**整个轮次列表静默渲染成空**
 * （`pageerror` 也是零）。jsdom 用例照不出这种"整树变空"，只有真浏览器能。
 *
 * 所以这条用例只做一件事：真建会话、真发消息、真等终态，然后断言页面上确实有
 * 一"轮"和一条用户消息。任何把轮次列表改空的改动都会在这里当场显形，
 * 而且失败信息直指根因（不像 `edit-message` 会卡在侧栏点行超时上）。
 */
test("轮次列表在真浏览器里确实渲染出内容（重构护栏）", async ({
  page,
  request,
}) => {
  const marker = `drill${Date.now()}`;
  const title = `Turn Render ${marker}`;
  const conversation = await createConversation(request, title);
  const question = `渲染冒烟提问 ${marker}`;
  const handle = await sendMessage(request, conversation.id, question);
  await waitForRunStatus(request, conversation.id, "completed", handle.laneId);

  await openConversation(page, title);
  await expectConversationHeading(page, title);

  // 一轮 = 一条用户消息 + 一条助手回答
  await expect(page.locator(".turn")).toHaveCount(1);
  await expect(page.locator(".user-copy")).toHaveCount(1);
  await expect(page.locator(".user-copy")).toContainText(question);

  // 出错时不留错误横幅（重构顺手改坏文案/结构的常见表现）
  await expect(page.getByRole("alert")).toHaveCount(0);
});
