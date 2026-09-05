import { expect, test } from "@playwright/test";
import {
  createConversation,
  openConversation,
  sendMessage,
  waitForRunStatus,
} from "../support/api";

test("消息操作：任意轮可复制；重新生成仅尾轮；尾轮重生成不丢历史（P0-2）", async ({
  page,
  request,
}) => {
  const title = `E2E Message Actions ${Date.now()}`;
  const conversation = await createConversation(request, title);
  for (const payload of ["用户消息一", "用户消息二", "用户消息三"]) {
    const handle = await sendMessage(request, conversation.id, payload);
    await waitForRunStatus(request, conversation.id, "completed", handle.laneId);
  }
  await openConversation(page, title);

  const turns = page.locator(".turn");
  await expect(turns).toHaveCount(3);

  // 历史轮（非最新）：可复制用户消息与回答，但不提供“重新生成”
  await expect(turns.nth(0).locator(".user-copy")).toHaveText("用户消息一");
  await expect(turns.nth(0).locator(".user-row-actions .copy-button")).toBeVisible();
  await expect(
    turns.nth(0).locator(".response-actions .copy-button"),
  ).toBeVisible();
  await expect(
    turns.nth(0).getByRole("button", { name: "重新生成" }),
  ).toHaveCount(0);

  // 用户消息复制进入“已复制”态
  const userCopy = turns.nth(1).locator(".user-row-actions .copy-button");
  await userCopy.click();
  await expect(userCopy).toHaveText("已复制");

  // 回答复制
  const answerCopy = turns.nth(1).locator(".response-actions .copy-button");
  await answerCopy.click();
  await expect(answerCopy).toHaveText("已复制");

  // 仅尾轮提供“重新生成”；点击后运行完成且三轮历史不丢
  const latestTurn = turns.nth(2);
  await expect(
    latestTurn.getByRole("button", { name: "重新生成" }),
  ).toBeEnabled();
  await expect(
    page.getByRole("button", { name: "重新生成", exact: true }),
  ).toHaveCount(1);

  await latestTurn.getByRole("button", { name: "重新生成" }).click();
  const regenerateButton = latestTurn.getByRole("button", {
    name: "重新生成",
  });
  // 运行期间按钮禁用；完成后恢复且无错误横幅、历史轮仍在
  await expect(regenerateButton).toBeEnabled({ timeout: 30_000 });
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.locator(".turn")).toHaveCount(3);
  await expect(page.locator(".user-copy")).toContainText(["用户消息一", "用户消息二"]);
});
