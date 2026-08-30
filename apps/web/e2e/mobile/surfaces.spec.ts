import { expect, test } from "@playwright/test";
import {
  createBranch,
  createCompletedConversation,
  listLanes,
  openConversation,
} from "../support/api";

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
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
});