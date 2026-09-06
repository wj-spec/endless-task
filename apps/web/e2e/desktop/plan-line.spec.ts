import { expect, test } from "@playwright/test";
import {
  createConversation,
  openConversation,
  sendMessage,
  waitForRunStatus,
} from "../support/api";

test("计划在运行后以可折叠计划行呈现于对话流（S-P1-3a）", async ({
  page,
  request,
}) => {
  const title = `E2E PlanLine ${Date.now()}`;
  const conversation = await createConversation(request, title);
  const handle = await sendMessage(request, conversation.id, "[e2e:plan]");
  await waitForRunStatus(request, conversation.id, "completed", handle.laneId);

  await openConversation(page, title);

  // 轮内 PlanLine：标题可见 + 完成摘要 + 展开可看步骤
  const stream = page.getByRole("log", { name: "对话记录" });
  const planLine = stream.locator(".plan-line").first();
  await expect(planLine).toBeVisible({ timeout: 15_000 });
  await expect(planLine).toContainText("E2E 测试计划");
  await expect(planLine.getByRole("button", { name: "展开计划" })).toBeVisible();

  await planLine.getByRole("button", { name: "展开计划" }).click();
  await expect(planLine).toContainText("步骤一：调研");
  await expect(planLine).toContainText("步骤二：起草");
  await expect(planLine).toContainText("待办");
  await expect(planLine).toContainText("进行中");
});
