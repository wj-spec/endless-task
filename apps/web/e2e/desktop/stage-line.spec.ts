import { expect, test } from "@playwright/test";
import {
  createConversation,
  openConversation,
  sendMessage,
} from "../support/api";

test("复杂运行中出现轻量阶段活动行；纯问答不打扰（S-A1）", async ({
  page,
  request,
}) => {
  // 复杂：模型声明计划后进入 in_progress 步骤，运行窗口内应出现阶段行
  const planTitle = `E2E Stage ${Date.now()}`;
  const planConversation = await createConversation(request, planTitle);
  const planHandle = await sendMessage(request, planConversation.id, "[e2e:plan]");
  await openConversation(page, planTitle);

  const stream = page.getByRole("log", { name: "对话记录" });
  const stage = stream.locator(".activity-stage").first();
  await expect(stage).toContainText("推进", { timeout: 8_000 });

  // 运行结束后阶段行收敛消失（计划行保留）
  await expect(stage).toBeHidden({ timeout: 15_000 });
  await expect(stream.locator(".plan-line").first()).toBeVisible();
  void planHandle;

  // 纯问答：不应出现任何阶段行
  const plainTitle = `E2E Stage Plain ${Date.now()}`;
  const plain = await createConversation(request, plainTitle);
  const plainHandle = await sendMessage(request, plain.id, "E2E 基线消息");
  await openConversation(page, plainTitle);
  await expect(stream.locator(".activity-stage")).toHaveCount(0, {
    timeout: 5_000,
  });
  void plainHandle;
});
