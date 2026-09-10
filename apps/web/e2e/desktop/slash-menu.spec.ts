import { expect, test } from "@playwright/test";
import {
  apiUrl,
  createCompletedConversation,
  createTemporaryConversationFromMenu,
  openConversation,
} from "../support/api";

/**
 * S14b 回归：`/` 候选浮层必须真的可见。
 *
 * 它原先绝对定位在输入行上方，而 `.composer` 是 `overflow: hidden`——
 * 于是整个菜单被祖先裁掉：用户只看到输入框自己的阴影，看不到任何候选
 * （jsdom 单测发现不了，因为没有布局/裁剪）。现在菜单 portal 到 body +
 * fixed 定位，这里就用真实布局断言它没被裁。
 */
const expectMenuVisibleOver = async (
  page: import("@playwright/test").Page,
  composer: import("@playwright/test").Locator,
  name: string,
) => {
  const menu = page.locator(".composer-slash-menu");
  await expect(menu).toBeVisible();
  const menuBox = await menu.boundingBox();
  const composerBox = await composer.boundingBox();
  expect(menuBox).not.toBeNull();
  expect(composerBox).not.toBeNull();
  // 未被裁成一条线（裁掉的典型表现就是"只剩阴影"，高度接近 0）。
  expect(menuBox!.height).toBeGreaterThan(24);
  // 菜单在输入框上方，且整块都在视口内。
  expect(menuBox!.y + menuBox!.height).toBeLessThanOrEqual(composerBox!.y + 1);
  expect(menuBox!.y).toBeGreaterThanOrEqual(0);
  expect(menuBox!.x).toBeGreaterThanOrEqual(0);
  expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(
    page.viewportSize()!.width,
  );
  // 命中测试：菜单中心点的最上层元素就在菜单里（没有被别的东西盖住）。
  const hit = await page.evaluate(() => {
    const node = document.querySelector(".composer-slash-menu");
    if (!node) return { inside: false };
    const rect = node.getBoundingClientRect();
    const top = document.elementFromPoint(
      rect.x + rect.width / 2,
      rect.y + rect.height / 2,
    );
    return { inside: Boolean(top && node.contains(top)) };
  });
  expect(hit.inside).toBe(true);
  await expect(menu.getByRole("option").first()).toContainText(`/${name}`);
  return menu;
};

test("`/` 候选浮层可见、不被输入框裁剪，且点击可写入草稿", async ({
  page,
  request,
}) => {
  // 造一个唯一命名的技能，保证候选可预测（不依赖跑测机器上装了哪些技能）。
  const name = `e2e-slash-${Date.now()}`;
  const created = await request.post(`${apiUrl}/skills/create`, {
    data: {
      name,
      description: "e2e 用例技能",
      body: "正文",
      scope: "user",
    },
  });
  expect(created.ok()).toBe(true);

  const title = `E2E Slash Menu ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);

  const composer = page.locator(".composer");
  const textarea = composer.getByLabel("给 Endless 发送消息");
  await textarea.click();
  await textarea.pressSequentially(`/${name}`);

  const menu = await expectMenuVisibleOver(page, composer, name);

  // 点击候选项：写入 `/技能名 ` 并关闭菜单（焦点仍在输入框里）。
  await menu.getByRole("option").first().click();
  await expect(textarea).toHaveValue(`/${name} `);
  await expect(menu).toHaveCount(0);
  await expect(textarea).toBeFocused();

  const removed = await request.delete(`${apiUrl}/skills/user/${name}`);
  expect(removed.ok()).toBe(true);
});

test("侧边面板里的 `/` 候选同样可见（同一套 portal 定位）", async ({
  page,
  request,
}) => {
  const name = `e2e-slash-side-${Date.now()}`;
  const created = await request.post(`${apiUrl}/skills/create`, {
    data: { name, description: "e2e 侧栏技能", body: "正文", scope: "user" },
  });
  expect(created.ok()).toBe(true);

  await page.setViewportSize({ width: 1440, height: 900 });
  const title = `E2E Slash Side ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);
  await createTemporaryConversationFromMenu(page);

  const sideComposer = page.locator(".chat-surface.is-side .composer");
  await expect(sideComposer).toBeVisible();
  const textarea = sideComposer.getByLabel("给 Endless 发送消息");
  await textarea.click();
  await textarea.pressSequentially(`/${name}`);

  await expectMenuVisibleOver(page, sideComposer, name);

  const removed = await request.delete(`${apiUrl}/skills/user/${name}`);
  expect(removed.ok()).toBe(true);
});
