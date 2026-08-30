import { expect, test } from "@playwright/test";
import { createCompletedConversation, openConversation } from "../support/api";

test("RowMenu 键盘导航与确认弹窗焦点形成闭环", async ({ page, request }) => {
  const title = `E2E A11y ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);

  const trigger = page.getByRole("button", { name: "更多会话操作" });
  await trigger.focus();
  await trigger.press("ArrowDown");

  const menu = page.getByRole("menu", { name: "更多会话操作" });
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitem", { name: "重命名" })).toBeFocused();
  await page.keyboard.press("End");
  await expect(menu.getByRole("menuitem", { name: "删除" })).toBeFocused();
  await page.keyboard.press("Enter");

  const dialog = page.getByRole("dialog", { name: "删除对话" });
  await expect(dialog).toHaveAttribute("aria-modal", "true");
  await expect(dialog.getByRole("button", { name: "取消" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByRole("button", { name: "永久删除" })).toBeFocused();
  await page.keyboard.press("Escape");

  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});