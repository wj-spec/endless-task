import { expect, test } from "@playwright/test";
import {
  createConversation,
  openConversation,
  sendMessage,
  waitForRunStatus,
} from "../support/api";

test("执行状态面板可查看失败运行自动导出的轨迹包（P2-1b）", async ({
  page,
  request,
}) => {
  const title = `E2E Trajectory ${Date.now()}`;
  const conversation = await createConversation(request, title);
  const handle = await sendMessage(request, conversation.id, "[e2e:fail]");
  await waitForRunStatus(request, conversation.id, "failed", handle.laneId);

  await openConversation(page, title);

  // 打开系统能力面板 → 执行状态 tab
  await page.getByLabel("当前对话模型", { exact: true }).selectOption("__manage__");
  const panel = page.getByRole("complementary", { name: "系统能力面板" });
  await expect(panel).toBeVisible();
  await panel.getByRole("tab", { name: "执行状态" }).click();

  const trajectory = panel.getByRole("region", { name: "轨迹包" });
  await expect(trajectory.getByText(handle.runId)).toBeVisible({ timeout: 15_000 });
  await trajectory
    .getByRole("button", { name: new RegExp(handle.runId) })
    .click();
  await expect(trajectory).toContainText("schemaVersion", { timeout: 10_000 });
});
