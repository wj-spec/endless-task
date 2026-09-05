import { expect, test } from "@playwright/test";
import {
  apiUrl,
  createConversation,
  openConversation,
} from "../support/api";

test.fixme(
  "G1 ② 正向（fixme：依赖 P1-2 事件回填/推送基础，见 13）",
  async ({
  page,
  request,
}) => {
  const title = `E2E Auto-Rollback Positive ${Date.now()}`;
  const conversation = await createConversation(request, title);
  // 先打开会话订阅事件流，再注入 fixture：新事件经 SSE/轮询推送到前端。
  await openConversation(page, title);

  const fixture = await request.post(
    `${apiUrl}/__e2e/fixtures/failed-auto-restored-run`,
    { data: { conversationId: conversation.id } },
  );
  expect(fixture.ok()).toBeTruthy();

  // 快照/事件回填依赖一次重新加载订阅。
  await page.reload();
  await openConversation(page, title);

  const notice = page.getByRole("status").filter({ hasText: "已自动回滚" });
  await expect(notice).toBeVisible({ timeout: 12000 });
  const text = await notice.innerText();
  expect(text).toContain("2");
  expect(text).toContain("个文件恢复");
});
