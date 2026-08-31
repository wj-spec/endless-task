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

test("工作区承载会话且输入操作保持在输入框内", async ({ page, request }) => {
  const title = `E2E Workspace Composer ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);

  const workspaceNavigation = page.getByRole("region", { name: "工作区" });
  await expect(workspaceNavigation).toBeVisible();
  const sessionList = workspaceNavigation.getByRole("navigation", {
    name: "会话列表",
  });
  await expect(
    sessionList.locator(".session-item-main").filter({ hasText: title }),
  ).toBeVisible();

  const composer = page.locator(".composer");
  await expect(composer.getByLabel("添加文本文件")).toBeVisible();
  const modelSelect = composer.getByLabel("当前对话模型", { exact: true });
  await expect(modelSelect).toBeVisible();
  await expect(modelSelect).toHaveJSProperty("tagName", "SELECT");
  await expect(composer.getByLabel("当前对话模型覆盖")).toHaveCount(0);
  await expect(composer.locator('input[placeholder*="模型"]')).toHaveCount(0);
  await expect(composer.getByRole("button", { name: "发送消息" })).toBeVisible();
});

test("键盘聚焦时控件显示可见焦点环", async ({ page, request }) => {
  const title = `E2E FocusVisible ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);

  await page.keyboard.press("Tab");
  const focused = await page.evaluate(() => {
    const element = document.activeElement;
    if (!(element instanceof HTMLElement)) return null;
    const style = getComputedStyle(element);
    return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth };
  });
  expect(focused).not.toBeNull();
  expect(focused!.outlineStyle).not.toBe("none");
  expect(parseFloat(focused!.outlineWidth)).toBeGreaterThan(0);
});

test("行菜单按 Escape 关闭并把焦点归还触发按钮", async ({ page, request }) => {
  const title = `E2E Popover Close ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);

  const trigger = page.getByRole("button", { name: "更多会话操作" });
  await trigger.focus();
  await trigger.press("ArrowDown");

  const menu = page.getByRole("menu", { name: "更多会话操作" });
  await expect(menu).toBeVisible();
  await expect(menu.getByRole("menuitem", { name: "重命名" })).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(menu).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("深色模式下运行状态以文字表达而非仅靠颜色", async ({ page, request }) => {
  await page.emulateMedia({ colorScheme: "dark" });
  const title = `E2E Dark State ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);

  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  const status = page.getByRole("status").filter({ hasText: "模型服务可用" });
  await expect(status).toBeVisible();
  await expect(status).toContainText("模型服务可用");
});

test("reduced-motion 关闭会话流的平滑滚动", async ({ page, request }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  const title = `E2E Reduced ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);

  const scrollBehavior = await page
    .locator(".conversation-stream")
    .first()
    .evaluate((element) => getComputedStyle(element).scrollBehavior);
  expect(scrollBehavior).toBe("auto");
});

test("中英混合内容不产生横向溢出", async ({ page, request }) => {
  const title = `E2E 中英mixed混合内容 ${Date.now()}`;
  const content =
    "Hello 世界，this is a mixed 长文本 with English 与中文，测试 1234567890 ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz";
  await createCompletedConversation(request, title, content);
  await openConversation(page, title);

  const stream = page.locator(".conversation-stream").first();
  const metrics = await stream.evaluate((element) => {
    const node = element as HTMLElement;
    const box = node.getBoundingClientRect();
    return { left: box.left, right: box.right, scrollWidth: node.scrollWidth, clientWidth: node.clientWidth };
  });
  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  expect(metrics.left).toBeGreaterThanOrEqual(-0.5);
  expect(metrics.right).toBeLessThanOrEqual(viewport!.width + 0.5);
  expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth);
});

test("200% 浏览器缩放代理：窄桌面视口下无横向溢出", async ({ page, request }) => {
  const title = `E2E Zoom ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);

  await page.setViewportSize({ width: 720, height: 900 });
  const metrics = await page.evaluate(() => ({
    client: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(metrics.scroll).toBeLessThanOrEqual(metrics.client);
});