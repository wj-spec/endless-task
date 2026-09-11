import { expect, test } from "@playwright/test";
import {
  createConversation,
  expectConversationHeading,
  openConversation,
  sendMessage,
  waitForRunStatus,
} from "../support/api";

/**
 * 运行时事件流的浏览器级冒烟（"drill"）。
 *
 * 为什么单列一条：拆 `useChatApplication` 的**运行时事件簇**（`applyRuntimeSnapshot` /
 * `applyRuntimeProductEvent` / `followRuntimeConversation` / `hydrateActiveTurn`）时，
 * 最容易坏的不是"列表空"这种明显症状，而是**事件不再被应用**：运行中不显示、
 * 回复不出现、终态不识别。jsdom 里要模拟整套 SSE 才照得出来，真浏览器一条就够。
 *
 * 关键设计（第一版写错过）：**必须在运行还没结束时打开页面**。等运行结束再打开时，
 * 助手回复是从会话快照读出来的，把 `applyRuntimeProductEvent` 整个短路掉也照样渲染——
 * 第一版的变异验证因此没红。改成"发完消息立刻进页面"之后，界面上的运行态与回复
 * 都只能由实时事件产生，护栏才真的守住这条路径。
 */
test("运行时事件流把进行中的运行与终态投影到界面（重构护栏）", async ({
  page,
  request,
}) => {
  const marker = `rt${Date.now()}`;
  const title = `Runtime Stream ${marker}`;
  const conversation = await createConversation(request, title);
  // 只提交消息（202），不等运行结束——下面立刻进页面，练习实时事件路径。
  const handle = await sendMessage(request, conversation.id, `流式冒烟 ${marker}`);

  await openConversation(page, title);
  await expectConversationHeading(page, title);

  // 运行中：轮次已经出现在界面上（来自 runtime 事件 / 乐观态，而不是等快照）。
  await expect(page.locator(".turn")).toHaveCount(1, { timeout: 15_000 });

  // 运行结束：回复非空 + 终态可识别（重新生成可用）。
  await waitForRunStatus(request, conversation.id, "completed", handle.laneId);
  await expect(page.locator(".assistant-row")).toHaveCount(1);
  await expect(page.locator(".assistant-content")).not.toBeEmpty();
  await expect(page.getByRole("button", { name: "重新生成" })).toBeEnabled({
    timeout: 20_000,
  });
  await expect(page.getByRole("alert")).toHaveCount(0);
});
