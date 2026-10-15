import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { createCompletedConversation, openConversation } from "../support/api";

/**
 * 边界：本 spec 只验「页签能打开 + 空态」，不真的派生一个子运行。
 * 真派生子运行要跑一次完整的委派执行（模型 + 子会话），成本高且不属于 Task 7
 * 的验收面——查看/取消的行为由 `DelegatedChildrenPanel.test.tsx` 的单测覆盖。
 */
const openTasksPanel = async (page: Page, request: APIRequestContext) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  const title = `E2E Delegated Children ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);

  await page
    .locator(".rail-footer")
    .getByRole("button", { name: "任务与通知" })
    .click();
  const panel = page.getByRole("complementary", { name: "任务与通知面板" });
  await expect(panel).toBeVisible();
  return panel;
};

test("子任务页签在任务与通知面板里可打开并显示空态", async ({ page, request }) => {
  const panel = await openTasksPanel(page, request);

  await panel.getByRole("tab", { name: "子任务" }).click();
  await expect(panel.getByRole("tab", { name: "子任务" })).toHaveAttribute(
    "aria-selected",
    "true",
  );

  await expect(panel.getByText("还没有子任务")).toBeVisible();
  await expect(
    panel.getByText("主 Agent 派生子代理后，这里会列出它们的进度和结果。"),
  ).toBeVisible();
});
