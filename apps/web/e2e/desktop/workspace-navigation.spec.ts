import { expect, test, type Page } from "@playwright/test";
import {
  createConversation,
  createCompletedWorkspaceConversation,
  createWorkspace,
  createWorkspaceConversation,
} from "../support/api";

const assertNoHorizontalOverflow = async (page: Page) => {
  const metrics = await page.evaluate(() => {
    const root = document.documentElement;
    return { client: root.clientWidth, scroll: root.scrollWidth };
  });
  expect(metrics.scroll).toBeLessThanOrEqual(metrics.client + 1);
};

const navFor = (page: Page) =>
  page
    .getByRole("region", { name: "工作区" })
    .getByRole("navigation", { name: "会话列表" });

const openConversation = async (
  page: Page,
  workspaceName: string,
  subtitle: string,
) => {
  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();

  const nav = navFor(page);
  const group = nav.locator(".workspace-item-main").filter({
    hasText: workspaceName,
  });
  await expect(group).toBeVisible();
  await group.click();
  await nav
    .locator(".session-item-main")
    .filter({ hasText: subtitle })
    .click();
};

test("多工作区嵌套且未绑定组为空", async ({ page, request }) => {
  const alpha = await createWorkspace(request, "项目 Alpha");
  const beta = await createWorkspace(request, "产品 Beta");

  await createCompletedWorkspaceConversation(
    request,
    alpha.id,
    `Alpha-需求梳理 ${Date.now()}`,
  );
  await createCompletedWorkspaceConversation(
    request,
    alpha.id,
    `Alpha-竞品分析 ${Date.now()}`,
  );
  await createCompletedWorkspaceConversation(
    request,
    beta.id,
    `Beta-发布检查 ${Date.now()}`,
  );

  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();

  const nav = navFor(page);
  // 展开两个工作区组：组可能因「自动打开活动会话」已展开，仅对尚未展开的组点击，
  // 避免把已展开组再点成收起。
  const alphaGroup = nav.locator(".workspace-item-main").filter({ hasText: "项目 Alpha" });
  const betaGroup = nav.locator(".workspace-item-main").filter({ hasText: "产品 Beta" });
  if ((await alphaGroup.getAttribute("aria-expanded")) === "false") {
    await alphaGroup.click();
  }
  if ((await betaGroup.getAttribute("aria-expanded")) === "false") {
    await betaGroup.click();
  }

  await expect(
    nav.locator(".session-item-main").filter({ hasText: "Alpha-需求梳理" }),
  ).toBeVisible();
  await expect(
    nav.locator(".session-item-main").filter({ hasText: "Alpha-竞品分析" }),
  ).toBeVisible();
  await expect(
    nav.locator(".session-item-main").filter({ hasText: "Beta-发布检查" }),
  ).toBeVisible();

  // 会话全部归属工作区，未绑定组不存在（无遗留未归属会话）。
  const unbound = nav.locator(".recent-session-list");
  await expect(
    unbound.locator(".session-item-main").filter({ hasText: "Alpha-需求梳理" }),
  ).toHaveCount(0);
  await expect(
    unbound.locator(".session-item-main").filter({ hasText: "Beta-发布检查" }),
  ).toHaveCount(0);
});

test("工作区组可用键盘展开与收起", async ({ page, request }) => {
  const ws = await createWorkspace(request, "键盘工作区");
  await createWorkspaceConversation(request, ws.id, `键盘会话 ${Date.now()}`);

  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();

  const nav = navFor(page);
  const group = nav.locator(".workspace-item-main").filter({
    hasText: "键盘工作区",
  });
  await expect(group).toBeVisible();
  await expect(group).toHaveAttribute("aria-expanded", "false");

  await group.focus();
  await page.keyboard.press("Enter");
  await expect(group).toHaveAttribute("aria-expanded", "true");
  await expect(
    nav.locator(".session-item-main").filter({ hasText: "键盘会话" }),
  ).toBeVisible();

  await group.focus();
  await page.keyboard.press("Enter");
  await expect(group).toHaveAttribute("aria-expanded", "false");
});

test("选中会话呈现 selected 态且表头显示工作区上下文", async ({
  page,
  request,
}) => {
  const title = `Alpha-上下文标题 ${Date.now()}`;
  const ws = await createWorkspace(request, "上下文工作区");
  await createWorkspaceConversation(request, ws.id, title);

  await openConversation(page, "上下文工作区", title);

  const nav = navFor(page);
  const active = nav
    .locator(".session-item-main")
    .filter({ hasText: title })
    .first();
  await expect(active).toHaveAttribute("aria-current", "page");
  await expect(active.locator("..")).toHaveClass(/is-active/);

  // 表头：主上下文为工作区名，次级文本为会话标题
  await expect(page.locator(".heading-focus-name")).toHaveText("上下文工作区");
  await expect(page.locator(".heading-sub")).toHaveText(title);
});

test("长标题在会话行内单行省略", async ({ page, request }) => {
  const longTitle = `这是一个非常长的中文标题，混排 English text with numbers 1234567890 用来验证是否在单行内被裁切省略${Date.now()}`;
  const ws = await createWorkspace(request, "长标题工作区");
  await createWorkspaceConversation(request, ws.id, longTitle);

  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();
  const nav = navFor(page);
  await nav.locator(".workspace-item-main").filter({ hasText: "长标题工作区" }).click();

  const row = nav.locator(".session-item-main").filter({ hasText: longTitle }).first();
  await expect(row).toBeVisible();
  const style = await row.locator("span").first().evaluate((node) => {
    const el = node as HTMLElement;
    return {
      textOverflow: getComputedStyle(el).textOverflow,
      whiteSpace: getComputedStyle(el).whiteSpace,
      clientWidth: el.clientWidth,
      scrollWidth: el.scrollWidth,
    };
  });
  expect(style.textOverflow).toBe("ellipsis");
  expect(style.whiteSpace).toBe("nowrap");
  expect(style.scrollWidth).toBeGreaterThan(0);
});

test("1280 与 1440 视口下工作区导航无横向溢出", async ({ page, request }) => {
  const ws = await createWorkspace(request, "视口工作区");
  await createWorkspaceConversation(request, ws.id, `视口会话 ${Date.now()}`);
  await createConversation(request, `视口最近 ${Date.now()}`);

  for (const [width, height] of [
    [1280, 800],
    [1440, 960],
  ] as const) {
    await page.setViewportSize({ width, height });
    await page.goto("/");
    await expect(page.getByText("模型服务可用").first()).toBeVisible();
    const nav = navFor(page);
    await nav
      .locator(".workspace-item-main")
      .filter({ hasText: "视口工作区" })
      .click();
    await expect(
      nav.locator(".session-item-main").filter({ hasText: "视口会话" }),
    ).toBeVisible();
    await assertNoHorizontalOverflow(page);
  }
});

test("切换到另一工作区会话后目标组自动展开且选中行可见", async ({
  page,
  request,
}) => {
  const alphaTitle = `自动展开-α ${Date.now()}`;
  const betaTitle = `自动展开-β ${Date.now()}`;
  const alpha = await createWorkspace(request, "自动展开 Alpha");
  const beta = await createWorkspace(request, "自动展开 Beta");
  await createWorkspaceConversation(request, alpha.id, alphaTitle);
  await createWorkspaceConversation(request, beta.id, betaTitle);

  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();

  const nav = navFor(page);

  // 打开第一个工作区会话：目标组展开、选中行可见。
  await nav
    .locator(".workspace-item-main")
    .filter({ hasText: "自动展开 Alpha" })
    .click();
  const alphaRow = nav
    .locator(".session-item-main")
    .filter({ hasText: alphaTitle });
  await expect(alphaRow).toBeVisible();
  await alphaRow.click();
  await expect(alphaRow).toHaveAttribute("aria-current", "page");

  // 切换到另一个工作区：目标组展开、新选中行可见且旧选中行取消。
  await nav
    .locator(".workspace-item-main")
    .filter({ hasText: "自动展开 Beta" })
    .click();
  const betaRow = nav
    .locator(".session-item-main")
    .filter({ hasText: betaTitle });
  await expect(betaRow).toBeVisible();
  await betaRow.click();
  await expect(betaRow).toHaveAttribute("aria-current", "page");
  await expect(alphaRow).not.toHaveAttribute("aria-current", "page");

  // 手动收起当前组（active 未变）：应保持收起，不被 effect 强制展开。
  const betaGroup = nav
    .locator(".workspace-item-main")
    .filter({ hasText: "自动展开 Beta" });
  await betaGroup.click();
  await expect(betaGroup).toHaveAttribute("aria-expanded", "false");
  // 表头仍指向活动会话，说明 active 未变，只做了手动收起。
  await expect(page.locator(".heading-sub")).toHaveText(betaTitle);
});

test("新建工作区必须先选择本地目录", async ({ page, request }) => {
  const ws = await createWorkspace(request, "占位工作区");
  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();

  // 打开创建对话框：不选目录直接提交 → 必须提示先选择目录。
  await page.getByRole("button", { name: "新建工作区" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByLabel("工作区名称").fill(`要求选目录 ${Date.now()}`);
  await dialog.getByRole("button", { name: "创建", exact: true }).click();
  await expect(
    dialog.getByText("请选择一个本地目录作为工作区根目录。"),
  ).toBeVisible();
});
