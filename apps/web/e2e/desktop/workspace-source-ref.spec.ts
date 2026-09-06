import { mkdirSync, writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import {
  createWorkspace,
  createWorkspaceConversation,
  sendMessage,
  waitForRunStatus,
} from "../support/api";

/**
 * 17 切片③：回答使用的工作区文件来源溯源 —— 读文件工具执行后，回答下方
 * 出现来源列表；点击条目打开预览抽屉并高亮行。
 */
test("工作区文件来源可溯源：来源列表 + 预览抽屉高亮行（17 切片③）", async ({
  page,
  request,
}) => {
  const ws = await createWorkspace(request, `来源工作区 ${Date.now()}`);
  expect(ws.rootPath).toBeTruthy();
  const root = ws.rootPath as string;
  mkdirSync(root, { recursive: true });
  const lines = Array.from(
    { length: 30 },
    (_, index) => `note line ${index + 1}`,
  );
  writeFileSync(`${root}/e2e-note.md`, `${lines.join("\n")}\n`, "utf-8");

  const title = `E2E Source Ref ${Date.now()}`;
  const conversation = await createWorkspaceConversation(
    request,
    ws.id,
    title,
  );

  // 先经 API 完成 run（与会话列表时序解耦），再打开页面查看溯源 UI。
  const handle = await sendMessage(
    request,
    conversation.id,
    "[e2e:workspace-ref] 请查看工作区文件后回答",
  );
  await waitForRunStatus(request, conversation.id, "completed", handle.laneId);

  // 全量共享 db 下工作区组很多：先定位并展开本会话所在组，再打开会话。
  await page.goto("/");
  await expect(page.getByText("模型服务可用").first()).toBeVisible();
  const nav = page.getByRole("navigation", { name: "会话列表" });
  const group = nav
    .locator(".workspace-item-main")
    .filter({ hasText: "来源工作区" });
  for (let guard = 0; guard < 20; guard += 1) {
    const expanded = await group.getAttribute("aria-expanded");
    if (expanded === "true") break;
    await group.click();
    await page.waitForTimeout(120);
  }
  const row = nav.locator(".session-item-main").filter({ hasText: title });
  await expect(row).toBeVisible({ timeout: 10_000 });
  await row.click();

  // 来源列表出现：1 个工作区文件 + 路径
  const refs = page.locator(".workspace-refs");
  await expect(refs).toBeVisible({ timeout: 10_000 });
  await expect(refs.getByText(/1 个工作区文件/)).toBeVisible();
  const item = refs.locator(".workspace-refs-item").first();
  await expect(item).toContainText("e2e-note.md");
  await expect(item).toContainText("L1");

  // 打开预览抽屉：标题=路径，行高亮存在
  await item.click();
  const drawer = page.locator(".workspace-preview-drawer");
  await expect(drawer).toBeVisible();
  await expect(drawer.locator(".workspace-preview-title")).toContainText(
    "e2e-note.md",
  );
  await expect(
    drawer.locator(".workspace-preview-line.is-highlight").first(),
  ).toBeVisible();
  await expect(
    drawer.locator(".workspace-preview-line").first(),
  ).toContainText("note line 1");

  // 关闭抽屉
  await drawer.getByRole("button", { name: "关闭文件预览" }).click();
  await expect(drawer).toBeHidden();
});
