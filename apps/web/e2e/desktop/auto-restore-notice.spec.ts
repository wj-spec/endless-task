import { expect, test } from "@playwright/test";
import {
  createConversation,
  openConversation,
  sendMessage,
  waitForRunStatus,
} from "../support/api";

test("G1 ② 负向：普通成功流程不出现「已自动回滚」提示", async ({
  page,
  request,
}) => {
  const title = `E2E No Auto-Rollback ${Date.now()}`;
  const conversation = await createConversation(request, title);
  const handle = await sendMessage(request, conversation.id, "帮我整理要点");
  await waitForRunStatus(request, conversation.id, "completed", handle.laneId);
  await sendMessage(request, conversation.id, "再补充一条");
  await waitForRunStatus(request, conversation.id, "completed", handle.laneId);
  await openConversation(page, title);

  const log = page.getByRole("log", { name: "对话记录" });
  await expect(log).toBeVisible();
  const body = await log.innerText();
  expect(body).not.toContain("已自动回滚");
  expect(body).not.toMatch(/本次失败的改动已自动恢复/);
  await expect(page.getByText("已自动回滚")).toHaveCount(0);
});
