import { expect, test } from "@playwright/test";
import {
  createConversation,
  openConversation,
  sendMessage,
  waitForRunStatus,
} from "../support/api";

test("执行面板以 span 时间线呈现失败运行的执行轨迹（S-P1-3b）", async ({
  page,
  request,
}) => {
  const title = `E2E Spans ${Date.now()}`;
  const conversation = await createConversation(request, title);
  const handle = await sendMessage(request, conversation.id, "[e2e:fail]");
  await waitForRunStatus(request, conversation.id, "failed", handle.laneId);

  await openConversation(page, title);
  await page.getByLabel("当前对话模型", { exact: true }).selectOption("__manage__");
  const panel = page.getByRole("complementary", { name: "系统能力面板" });
  await expect(panel).toBeVisible();
  await panel.getByRole("tab", { name: "执行状态" }).click();

  const timeline = panel.getByRole("region", { name: "执行轨迹" });
  await expect(timeline.locator("li")).toHaveCount(2, { timeout: 15_000 });
  await expect(timeline).toContainText("运行");
  await expect(timeline).toContainText("模型轮");
  await expect(timeline).toContainText("失败");
});
