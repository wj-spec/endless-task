import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  createBranch,
  createCompletedConversation,
  listLanes,
  openConversation,
  expectConversationHeading,
} from "../support/api";

const useCompactViewport = (page: Page) =>
  page.setViewportSize({ width: 390, height: 844 });

const expectWithinViewport = async (page: Page, locator: Locator) => {
  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  await expect(locator).toBeVisible();

  const metrics = await locator.evaluate((element) => {
    const node = element as HTMLElement;
    const box = node.getBoundingClientRect();
    return {
      clientWidth: node.clientWidth,
      left: box.left,
      right: box.right,
      scrollWidth: node.scrollWidth,
    };
  });

  expect(metrics.left).toBeGreaterThanOrEqual(-0.5);
  expect(metrics.right).toBeLessThanOrEqual(viewport!.width + 0.5);
  expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth);
};

const expectSingleModalDialog = async (page: Page) => {
  await expect(page.locator('[role="dialog"][aria-modal="true"]')).toHaveCount(1);
};

test("390px 下聊天主路径完整位于视口内", async ({ page, request }) => {
  await useCompactViewport(page);
  const title = `E2E Mobile Width ${"长标题".repeat(12)} ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title, { mobile: true });

  const documentWidth = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  expect(documentWidth.scrollWidth).toBeLessThanOrEqual(documentWidth.clientWidth);

  await expectWithinViewport(page, page.locator(".chat-surface").first());
  await expectWithinViewport(page, page.locator(".surface-header").first());
  await expectWithinViewport(page, page.locator(".composer").first());
  await expectWithinViewport(page, page.getByLabel("给 Endless 发送消息"));
});

test("移动端核心聊天操作保持 44px 触控热区", async ({ page, request }) => {
  await useCompactViewport(page);
  const title = `E2E Mobile Targets ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title, { mobile: true });

  const controls: Array<[string, Locator]> = [
    ["展开侧栏", page.locator(".mobile-menu")],
    ["全局搜索", page.getByRole("button", { name: "全局搜索" })],
    ["更多会话操作", page.getByRole("button", { name: "更多会话操作" })],
    ["添加文本文件", page.getByRole("button", { name: "添加文本文件" })],
    ["发送消息", page.getByRole("button", { name: "发送消息" })],
  ];

  for (const [name, control] of controls) {
    await expect(control, `${name} 应可见`).toBeVisible();
    const box = await control.boundingBox();
    expect(box, `${name} 应有可测量的边界`).not.toBeNull();
    expect(box!.width, `${name} 宽度`).toBeGreaterThanOrEqual(44);
    expect(box!.height, `${name} 高度`).toBeGreaterThanOrEqual(44);
  }
});

test("移动会话抽屉限制焦点并在关闭后归还", async ({ page }) => {
  await useCompactViewport(page);
  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();

  const trigger = page.locator(".mobile-menu");
  await trigger.click();

  const dialog = page.getByRole("dialog", { name: "会话导航" });
  const closeButton = dialog.getByRole("button", { name: "收起侧栏" });
  await expect(dialog).toBeVisible();
  await expect(closeButton).toBeFocused();

  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByRole("button", { name: "设置" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(closeButton).toBeFocused();

  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("移动端 Branch 使用全屏次级工作面并可退出", async ({ page, request }) => {
  const title = `E2E Mobile ${Date.now()}`;
  const { conversation } = await createCompletedConversation(request, title);
  const lanes = await listLanes(request, conversation.id);
  await createBranch(request, conversation.id, lanes.mainLaneId!, "移动端分支");
  await openConversation(page, title, { mobile: true });

  await page.getByRole("button", { name: /^主线/ }).click();
  await page.getByRole("button", { name: "管理分支：移动端分支" }).click();
  await page.getByRole("menuitem", { name: "右侧对照" }).click();

  const side = page.getByRole("region", { name: "分支对照" });
  await expect(side).toBeVisible();
  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  await expect
    .poll(async () => {
      const box = await side.boundingBox();
      return Boolean(
        box &&
          box.x <= 1 &&
          box.y <= 1 &&
          box.width >= viewport!.width - 1 &&
          box.height >= viewport!.height - 1,
      );
    })
    .toBe(true);
  await expect(side.getByRole("button", { name: "收起分支对照" })).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(side).toBeHidden();
  await expectConversationHeading(page, title);
});

const focusedInsideOpenModal = (page: Page) =>
  page.evaluate(() => {
    const dialog = document.querySelector('[role="dialog"][aria-modal="true"]');
    return Boolean(dialog && dialog.contains(document.activeElement));
  });

const documentHasNoHorizontalOverflow = (page: Page) =>
  page.evaluate(() => {
    const root = document.documentElement;
    return { client: root.clientWidth, scroll: root.scrollWidth };
  });

test("辅助表面互斥：会话列表与助手面板不同时出现", async ({ page }) => {
  await useCompactViewport(page);
  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();

  await page.locator(".mobile-menu").click();
  const rail = page.getByRole("dialog", { name: "会话导航" });
  await expect(rail).toBeVisible();
  await expectSingleModalDialog(page);

  await rail.getByRole("button", { name: "任务与通知" }).click();
  const assistant = page.getByRole("dialog", { name: "任务与通知面板" });
  await expect(assistant).toBeVisible();
  await expect(rail).toBeHidden();
  await expectSingleModalDialog(page);

  await page.keyboard.press("Escape");
  await expect(assistant).toBeHidden();
});

test("辅助表面互斥：会话列表与设置表面不同时出现", async ({ page }) => {
  await useCompactViewport(page);
  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();

  await page.locator(".mobile-menu").click();
  const rail = page.getByRole("dialog", { name: "会话导航" });
  await expect(rail).toBeVisible();

  await rail.getByRole("button", { name: "设置" }).click();
  const settings = page.getByRole("dialog", { name: "设置" });
  await expect(settings).toBeVisible();
  await expect(rail).toBeHidden();
  await expectSingleModalDialog(page);

  await page.keyboard.press("Escape");
  await expect(settings).toBeHidden();
});

test("助手面板移动模态限制焦点并在关闭后归还", async ({ page }) => {
  await useCompactViewport(page);
  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();

  await page.locator(".mobile-menu").click();
  await page
    .getByRole("dialog", { name: "会话导航" })
    .getByRole("button", { name: "任务与通知" })
    .click();
  const assistant = page.getByRole("dialog", { name: "任务与通知面板" });
  await expect(assistant).toBeVisible();
  expect(await focusedInsideOpenModal(page)).toBe(true);

  for (let index = 0; index < 8; index += 1) {
    await page.keyboard.press("Tab");
    expect(await focusedInsideOpenModal(page)).toBe(true);
  }
  await page.keyboard.press("Shift+Tab");
  expect(await focusedInsideOpenModal(page)).toBe(true);

  await page.keyboard.press("Escape");
  await expect(assistant).toBeHidden();
  await expect(page.locator("[role='dialog'][aria-modal='true']")).toHaveCount(0);
});

test("设置表面移动模态限制焦点", async ({ page }) => {
  await useCompactViewport(page);
  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();

  await page.locator(".mobile-menu").click();
  await page
    .getByRole("dialog", { name: "会话导航" })
    .getByRole("button", { name: "设置" })
    .click();
  const settings = page.getByRole("dialog", { name: "设置" });
  await expect(settings).toBeVisible();
  expect(await focusedInsideOpenModal(page)).toBe(true);

  for (let index = 0; index < 8; index += 1) {
    await page.keyboard.press("Tab");
    expect(await focusedInsideOpenModal(page)).toBe(true);
  }

  await page.keyboard.press("Escape");
  await expect(settings).toBeHidden();
});

test("切换辅助表面不产生横向溢出", async ({ page }) => {
  await useCompactViewport(page);
  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();

  const assertNoOverflow = async () => {
    const { client, scroll } = await documentHasNoHorizontalOverflow(page);
    expect(scroll).toBeLessThanOrEqual(client);
  };

  await page.locator(".mobile-menu").click();
  await expect(page.getByRole("dialog", { name: "会话导航" })).toBeVisible();
  await assertNoOverflow();
  await page.keyboard.press("Escape");

  await page.locator(".mobile-menu").click();
  await page
    .getByRole("dialog", { name: "会话导航" })
    .getByRole("button", { name: "任务与通知" })
    .click();
  await expect(page.getByRole("dialog", { name: "任务与通知面板" })).toBeVisible();
  await assertNoOverflow();
  await page.keyboard.press("Escape");

  await page.locator(".mobile-menu").click();
  await page
    .getByRole("dialog", { name: "会话导航" })
    .getByRole("button", { name: "设置" })
    .click();
  await expect(page.getByRole("dialog", { name: "设置" })).toBeVisible();
  await assertNoOverflow();
});