import { expect, test, type Page } from "@playwright/test";
import {
  apiUrl,
  createCompletedWorkspaceConversation,
  createWorkspace,
  createWorkspaceConversation,
  openConversation,
} from "../support/api";

/**
 * 批次 H / F-1：**已绑定但还没有对话的工作区是死角**。
 *
 * 用户实测：「新建的工作区没有『新建会话』按钮」。
 * 根因链（`h-brief.md` §1①）：`SessionRail` 的 `currentWorkspaceId` **优先取活动会话所属
 * 工作区**，所以只要还开着一条旧会话，新工作区永远不是 current；行内新建按钮又只给
 * current；空态没给动作。⇒ 该工作区的第一条对话**没有任何入口**。
 *
 * 这里跑真前端 + 真后端（隔离的 18000 端口 / 临时库），走的就是用户点得到的那条路：
 * 旧工作区有会话并成为 current → 空白的新工作区行上必须能建出它的第一段对话，
 * 且**真的建在那个工作区里**（不是被 current 抢走）。
 *
 * ⚠️ 侧栏的工作区列表只在挂载时拉一次（`useWorkspaceNavigation`）：工作区必须
 * **在 `page.goto` 之前**建好，否则 UI 里根本不会有那一行（本 spec 第一版三个用例
 * 全红就是这个原因——不是产品 bug）。
 */
const navFor = (page: Page) => page.getByRole("navigation", { name: "会话列表" });

const groupFor = (page: Page, workspaceId: string) =>
  navFor(page).locator(`[data-workspace-id="${workspaceId}"]`);

const ensureExpanded = async (page: Page, workspaceId: string) => {
  const toggle = groupFor(page, workspaceId).locator(".workspace-item-main");
  await expect(toggle).toBeVisible();
  if ((await toggle.getAttribute("aria-expanded")) === "false") {
    await toggle.click();
  }
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
};

const activeConversationsOf = (
  request: Parameters<typeof createWorkspace>[0],
  workspaceId: string,
) =>
  request
    .get(`${apiUrl}/conversations?status=active&workspace=${workspaceId}`)
    .then((response) => response.json());

test("空白工作区也能建出第一段对话，且建在它自己名下", async ({
  page,
  request,
}) => {
  // 旧工作区先放一段对话：它因此成为 current（currentWorkspaceId 优先活动会话）。
  const oldWs = await createWorkspace(request, `旧工作区 ${Date.now()}`);
  const oldTitle = `旧对话 ${Date.now()}`;
  await createCompletedWorkspaceConversation(request, oldWs.id, oldTitle);
  // 新工作区已绑定、**没有任何对话**；必须在页面加载前建好。
  const emptyWs = await createWorkspace(request, `空白工作区 ${Date.now()}`);

  await openConversation(page, oldTitle, { workspaceId: oldWs.id });
  await ensureExpanded(page, emptyWs.id);
  const emptyGroup = groupFor(page, emptyWs.id);

  // 死角复现点：旧实现里这一行**什么都没有**（按钮只在 current 显示）。
  const inline = emptyGroup.getByRole("button", { name: "在此工作区新建对话" });
  await expect(inline).toBeVisible();
  await inline.click();

  // 建出来的对话**真的落在空工作区名下**，并且已经打开成当前对话。
  // （口径说明：后端 `create_or_reuse_empty_conversation` 对「同工作区已有空闲对话」
  //   会复用而不新插一行，所以这里断言的是「该工作区里有一段 title=新对话 的对话 +
  //   主区指向它」，不断言行数增减 —— 那是后端契约，不是本批改的东西。）
  await expect(
    emptyGroup.locator(".session-item-main").filter({ hasText: "新对话" }),
  ).toHaveCount(1);
  await expect(page.locator(".heading-sub, .heading-title").first()).toHaveText(
    "新对话",
  );

  const fresh = await activeConversationsOf(request, emptyWs.id);
  expect(fresh.items).toHaveLength(1);
  expect(fresh.items[0].title).toBe("新对话");
  expect(fresh.items[0].workspaceId).toBe(emptyWs.id);
});

test("空白工作区的空态给出「新建对话」动作，点它同样建在它自己名下", async ({
  page,
  request,
}) => {
  const oldWs = await createWorkspace(request, `旧工作区2 ${Date.now()}`);
  const oldTitle = `旧对话2 ${Date.now()}`;
  await createCompletedWorkspaceConversation(request, oldWs.id, oldTitle);
  const emptyWs = await createWorkspace(request, `空白工作区2 ${Date.now()}`);

  await openConversation(page, oldTitle, { workspaceId: oldWs.id });
  await ensureExpanded(page, emptyWs.id);
  const emptyGroup = groupFor(page, emptyWs.id);

  const emptyState = emptyGroup.locator(".empty-state.rail-empty");
  await expect(emptyState).toBeVisible();
  await expect(emptyState).toContainText("在这里新建一段对话");
  await emptyState.getByRole("button", { name: "新建对话" }).click();

  await expect(
    emptyGroup.locator(".session-item-main").filter({ hasText: "新对话" }),
  ).toHaveCount(1);
  const fresh = await activeConversationsOf(request, emptyWs.id);
  expect(fresh.items).toHaveLength(1);
  expect(fresh.items[0].title).toBe("新对话");
  expect(fresh.items[0].workspaceId).toBe(emptyWs.id);
});

test("有对话的工作区行上也有「新建对话」，建出的仍归它", async ({
  page,
  request,
}) => {
  const ws = await createWorkspace(request, `有对话工作区 ${Date.now()}`);
  const title = `已有对话 ${Date.now()}`;
  await createWorkspaceConversation(request, ws.id, title);

  await openConversation(page, title, { workspaceId: ws.id });
  const group = groupFor(page, ws.id);
  await ensureExpanded(page, ws.id);
  await expect(group.locator(".session-item-main")).toHaveCount(1);

  // 这一行**本来就是 current**（活动会话在它里面）：老实现也有「+」，所以这条是
  // F-1① 的**正向对照**——修「非当前也有」不许把「当前有」弄丢。
  await group.getByRole("button", { name: "在此工作区新建对话" }).click();
  // 该工作区里已有一段对话 ⇒ 后端复用（见上），所以标题仍是它、行数仍是 1；
  // 这里钉的是「入口在 current 行上也可用，并且点完主区聚焦到本工作区的对话」。
  await expect(page.locator(".heading-sub, .heading-title").first()).toContainText(
    title,
  );
  await expect(
    group.locator(".session-item-main").filter({ hasText: title }),
  ).toHaveCount(1);
});
