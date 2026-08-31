import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { createCompletedConversation, openConversation } from "../support/api";

const AXIS_TOLERANCE = 40;

const centerX = async (page: Page, selector: string) => {
  const box = await page.locator(selector).first().boundingBox();
  expect(box, `${selector} 应有可测量边界`).not.toBeNull();
  return box!.x + box!.width / 2;
};

const assertNoHorizontalOverflow = async (page: Page) => {
  const metrics = await page.evaluate(() => {
    const root = document.documentElement;
    return { client: root.clientWidth, scroll: root.scrollWidth };
  });
  expect(metrics.scroll).toBeLessThanOrEqual(metrics.client + 1);
};

const seedAndOpen = async (page: Page, request: APIRequestContext) => {
  const title = `E2E Layout ${Date.now()}`;
  await createCompletedConversation(request, title);
  await openConversation(page, title);
  await page.waitForTimeout(300);
  return title;
};

test("1440×960 无横向溢出且阅读列与 Composer 同轴", async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await seedAndOpen(page, request);

  await assertNoHorizontalOverflow(page);

  const messageColumn = page.locator(".message-column").first();
  const composer = page.locator(".composer").first();
  await expect(messageColumn).toBeVisible();
  await expect(composer).toBeVisible();

  const readingWidth = (await messageColumn.boundingBox())!.width;
  expect(readingWidth).toBeGreaterThanOrEqual(600);

  const [readingCenter, composerCenter] = await Promise.all([
    centerX(page, ".message-column"),
    centerX(page, ".composer"),
  ]);
  expect(Math.abs(readingCenter - composerCenter)).toBeLessThanOrEqual(AXIS_TOLERANCE);

  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  const composerBox = (await composer.boundingBox())!;
  expect(composerBox.x).toBeGreaterThanOrEqual(0);
  expect(composerBox.x + composerBox.width).toBeLessThanOrEqual(viewport!.width + 1);
});

test("1280×800 无横向溢出且主工作面可用", async ({ page, request }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await seedAndOpen(page, request);

  await assertNoHorizontalOverflow(page);

  const composer = page.locator(".composer").first();
  await expect(composer).toBeVisible();
  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  const composerBox = (await composer.boundingBox())!;
  expect(composerBox.x).toBeGreaterThanOrEqual(0);
  expect(composerBox.x + composerBox.width).toBeLessThanOrEqual(viewport!.width + 1);

  const [readingCenter, composerCenter] = await Promise.all([
    centerX(page, ".message-column"),
    centerX(page, ".composer"),
  ]);
  expect(Math.abs(readingCenter - composerCenter)).toBeLessThanOrEqual(AXIS_TOLERANCE);
});

test("rail 折叠后主工作面仍可用且可恢复", async ({ page, request }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await seedAndOpen(page, request);

  const rail = page.locator(".session-rail");
  await expect(rail).toBeVisible();

  await page.getByRole("button", { name: "收起侧栏" }).click();
  await expect(rail).toHaveClass(/is-collapsed/);
  await assertNoHorizontalOverflow(page);
  await expect(page.locator(".composer").first()).toBeVisible();

  await page.getByRole("button", { name: "展开侧栏" }).click();
  await expect(rail).not.toHaveClass(/is-collapsed/);
  await assertNoHorizontalOverflow(page);
});

test("桌面助手面板为非模态且不遮挡主工作面", async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 960 });
  await seedAndOpen(page, request);

  await page.locator(".rail-footer").getByRole("button", { name: "任务与通知" }).click();

  const assistant = page.locator(".assistant-panel");
  await expect(assistant).toBeVisible();

  // 桌面 persistent panel 不是 modal：不设置 aria-modal / dialog 语义
  const ariaModal = await assistant.getAttribute("aria-modal");
  expect(ariaModal).toBeNull();

  // 主工作面与 composer 不被遮挡、仍在视口内
  await expect(page.locator(".composer").first()).toBeVisible();
  await assertNoHorizontalOverflow(page);

  const viewport = page.viewportSize();
  expect(viewport).not.toBeNull();
  const composerBox = (await page.locator(".composer").first().boundingBox())!;
  expect(composerBox.x + composerBox.width).toBeLessThanOrEqual(viewport!.width + 1);
});

test("light/dark 下 rail/main/aux 表面可区分", async ({ page, request }) => {
  for (const colorScheme of ["light", "dark"]) {
    await page.emulateMedia({ colorScheme });
    await page.setViewportSize({ width: 1280, height: 800 });
    await seedAndOpen(page, request);

    await page.locator(".rail-footer").getByRole("button", { name: "任务与通知" }).click();

    const bg = async (selector: string) =>
      page.locator(selector).first().evaluate((el) => {
        // rgba() 由 background 计算值得到，含颜色分量即可比较
        return getComputedStyle(el as HTMLElement).backgroundColor;
      });

    const railBg = await bg(".session-rail");
    const mainBg = await bg(".chat-surface");
    const auxBg = await bg(".assistant-panel");

    expect([railBg, mainBg, auxBg].every((v) => v && v !== "rgba(0, 0, 0, 0)")).toBe(true);
    expect(railBg).not.toBe(mainBg);
  }
});