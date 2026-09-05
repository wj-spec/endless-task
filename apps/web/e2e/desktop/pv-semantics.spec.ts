import { expect, test } from "@playwright/test";
import {
  createConversation,
  openConversation,
  sendMessage,
  waitForRunStatus,
} from "../support/api";

test("产品语义收口：普通对话流不暴露内部 id/英文状态（P0-3）", async ({
  page,
  request,
}) => {
  const title = `E2E PV Semantics ${Date.now()}`;
  const conversation = await createConversation(request, title);
  for (const payload of ["第一段问题", "第二段问题", "第三段问题"]) {
    const handle = await sendMessage(request, conversation.id, payload);
    await waitForRunStatus(request, conversation.id, "completed", handle.laneId);
  }
  await openConversation(page, title);

  const log = page.getByRole("log", { name: "对话记录" });
  await expect(log).toBeVisible();
  const body = await log.innerText();

  // 普通对话流不得出现内部 id / v2 技术词 / 英文运行状态 / “工具执行 <id>：<status>” 旧文案
  expect(body).not.toMatch(/\b(runId|laneId|variantId|toolExecutionId|callId|seq)\b/);
  expect(body).not.toMatch(/\b(created|queued|running|completed|failed|cancelled|rejected|expired)\b/);
  expect(body).not.toMatch(/工具执行\s+[\w:-]+[：:]/);

  // 用户消息与回答均按产品文案呈现
  await expect(page.getByText("第一段问题", { exact: true })).toBeVisible();
  await expect(page.getByText("E2E 确定性回复", { exact: true }).first()).toBeVisible();
});
