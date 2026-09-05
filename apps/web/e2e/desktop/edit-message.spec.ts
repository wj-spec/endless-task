import { expect, test } from "@playwright/test";
import {
  createConversation,
  expectConversationHeading,
  openConversation,
  sendMessage,
  waitForRunStatus,
} from "../support/api";

test("编辑最新消息并重新生成（P0-2c）", async ({ page, request }) => {
  const marker = `editmsg${Date.now()}`;
  const title = `Edit Message ${marker}`;
  const conversation = await createConversation(request, title);
  const original = `原始提问 ${marker}`;
  const handle = await sendMessage(request, conversation.id, original);
  await waitForRunStatus(request, conversation.id, "completed", handle.laneId);
  await openConversation(page, title);
  await expectConversationHeading(page, title);

  const userCopy = page.locator(".user-copy");
  await expect(userCopy).toContainText(original);

  // 最新轮用户消息提供“编辑”
  await page.getByRole("button", { name: "编辑" }).click();
  const editor = page.getByLabel("编辑这条消息");
  await expect(editor).toBeVisible();

  const edited = `修改后的提问 ${marker}`;
  await editor.fill(edited);
  await page.getByRole("button", { name: "保存并重新生成" }).click();

  // 用户行即时显示改写文案；运行完成且无错误横幅
  await expect(userCopy.first()).toContainText(edited, { timeout: 15_000 });
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "重新生成" })).toBeEnabled({
    timeout: 30_000,
  });
  await expect(page.getByRole("alert")).toHaveCount(0);
});
