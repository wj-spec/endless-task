import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { apiUrl, createCompletedConversation, openConversation } from "../support/api";

/**
 * P1-2 hub SSE 推送 E2E：后台落库通知 + notification.created hub 事件后，
 * 角标应经 SSE 即时刷新出现（不等 10s 轮询兜底）。
 */
test("hub SSE 推送：通知产生后未读角标即时出现（P1-2）", async ({
  page,
  request,
}) => {
  const title = `E2E Hub Push ${Date.now()}`;
  const { conversation } = await createCompletedConversation(request, title);
  await openConversation(page, title);
  await page.waitForTimeout(300);

  const railButton = page
    .locator(".rail-footer")
    .getByRole("button", { name: "任务与通知" });

  // 初始角标计数（0 或共享 db 中既有 pending 数），seed 后应 +1。
  const strong = railButton.locator("strong");
  const initialBadge =
    (await strong.count()) > 0 ? await strong.first().textContent() : "0";

  // 后端按生产写点路径落库通知 + append hub 事件。
  const seed = await request.post(`${apiUrl}/__e2e/notifications`, {
    data: { conversationId: conversation.id },
  });
  expect(seed.ok()).toBeTruthy();

  // 推送应即时到达（远快于 10s 轮询兜底）；用短超时证明走 SSE。
  const expected = String((Number(initialBadge) || 0) + 1);
  await expect(strong).toHaveText(expected, {
    timeout: 4_000,
  });

  // 打开助手面板可见该通知标题（数据经同一刷新面到达）。
  await railButton.click();
  const assistant = page.locator(".assistant-panel");
  await expect(assistant).toBeVisible();
  await expect(assistant.getByText(/E2E 后台任务完成/)).toBeVisible();

  // 清理：read-all 让后续 spec 不残留未读通知（本 spec 不 seed proposal）。
  await request.post(`${apiUrl}/notifications/read-all`);
});
