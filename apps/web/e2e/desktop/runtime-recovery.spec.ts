import { expect, test } from "@playwright/test";
import {
  apiUrl,
  createConversation,
  openConversation,
  runtimeSnapshot,
  expectConversationHeading,
} from "../support/api";

test("普通聊天完成后刷新仍恢复同一运行结果", async ({ page, request }) => {
  const title = `E2E 刷新恢复 ${Date.now()}`;
  const conversation = await createConversation(request, title);
  await openConversation(page, title);

  const composer = page.getByRole("textbox", { name: "给 Endless 发送消息" });
  await composer.fill("刷新后仍应看到这条消息");
  await page.getByRole("button", { name: "发送消息" }).click();

  await expect(page.getByText("刷新后仍应看到这条消息")).toBeVisible();
  await expect(page.getByText("E2E 确定性回复")).toBeVisible();
  await expect(page.getByRole("button", { name: "发送消息" })).toBeVisible();

  await page.reload();
  await expectConversationHeading(page, title);
  await expect(page.getByText("刷新后仍应看到这条消息")).toBeVisible();
  await expect(page.getByText("E2E 确定性回复")).toBeVisible();

  const snapshot = await runtimeSnapshot(request, conversation.id);
  expect(snapshot.runState?.status).toBe("completed");
});

test("活动运行在刷新和 SSE 断线后只重连原运行", async ({
  page,
  request,
}) => {
  const title = `E2E SSE 恢复 ${Date.now()}`;
  const conversation = await createConversation(request, title);
  await openConversation(page, title);

  const composer = page.getByRole("textbox", { name: "给 Endless 发送消息" });
  await composer.fill("[e2e:pause] 保持同一个运行");
  await page.getByRole("button", { name: "发送消息" }).click();

  await expect(page.getByText("E2E 确定性回复")).toBeVisible();
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "正在回答" })).toBeVisible();
  await expect(page.getByRole("main").locator(".runtime-recovery-card")).toHaveCount(0);
  await expect
    .poll(async () => {
      const response = await request.get(`${apiUrl}/__e2e/provider`);
      return (await response.json()).pausedCount as number;
    })
    .toBe(1);

  const beforeRefresh = await runtimeSnapshot(request, conversation.id);
  expect(beforeRefresh.runState?.status).toBe("running");

  let allowEventStream = false;
  await page.route(
    `**/api/v2/conversations/${conversation.id}/events?**`,
    async (route) => {
      if (allowEventStream) await route.continue();
      else await route.abort("connectionreset");
    },
  );
  await page.reload();
  await expect(page.getByText("E2E 确定性回复")).toBeVisible();
  await expect(page.getByRole("button", { name: "停止生成" })).toBeVisible();
  await expect(page.getByRole("status").filter({ hasText: "正在恢复连接" })).toBeVisible();
  allowEventStream = true;

  const resume = await request.post(`${apiUrl}/__e2e/provider/resume`);
  expect(resume.ok()).toBeTruthy();
  expect((await resume.json()).resumedCount).toBe(1);

  await expect(page.getByText("E2E 确定性回复，恢复后完成")).toBeVisible();
  await expect(page.getByRole("button", { name: "发送消息" })).toBeVisible();
  const afterRecovery = await runtimeSnapshot(request, conversation.id);
  expect(afterRecovery.runState?.status).toBe("completed");
});

test("Runtime Controller 按 Conversation 序号忽略重复和乱序事件", async ({
  page,
}) => {
  await page.goto("/");
  const projection = await page.evaluate(async () => {
    const { runtimeControllerReducer, runtimeTargetKey } = await import(
      "/src/features/chat/runtimeController.ts"
    );
    const conversationId = "conv_event_order";
    const laneId = "lane_main";
    const runId = "run_event_order";
    const snapshot = {
      snapshotVersion: 1,
      conversationId,
      activeLaneId: laneId,
      mainLaneId: laneId,
      runningLaneId: laneId,
      runningRunId: runId,
      activeRunId: runId,
      activeRunVariantId: runId,
      lastEventSeq: 10,
      entries: [],
      runState: {
        runId,
        status: "running",
        partialContent: "基线",
        errorCode: null,
        safeMessage: null,
        modelTurns: [],
      },
      pendingApprovals: [],
      toolStates: [],
      contextUsage: { inputTokens: 0, outputTokens: 0 },
      interruptedRuns: [],
      capabilities: [],
    };
    let state = {
      snapshots: {},
      events: {},
      connections: {},
      commands: {},
      lastEventSequences: {},
      latestEvent: null,
    };
    const mainTarget = { conversationId, laneId: null };
    const sideTarget = { conversationId, laneId };
    state = runtimeControllerReducer(state, {
      type: "snapshot_received",
      target: mainTarget,
      snapshot,
    });
    state = runtimeControllerReducer(state, {
      type: "snapshot_received",
      target: sideTarget,
      snapshot: { ...snapshot },
    });

    const event = (eventId: string, eventSeq: number, delta: string) => ({
      eventId,
      eventSeq,
      type: "message.updated",
      conversationId,
      laneId,
      runId,
      createdAt: "2026-03-14T00:00:00.000Z",
      data: { delta },
    });
    state = runtimeControllerReducer(state, {
      type: "event_received",
      event: event("evt_12", 12, " + 唯一增量"),
    });
    state = runtimeControllerReducer(state, {
      type: "event_received",
      event: event("evt_12_duplicate", 12, " + 重复增量"),
    });
    state = runtimeControllerReducer(state, {
      type: "event_received",
      event: event("evt_11_late", 11, " + 迟到增量"),
    });

    return {
      eventCount: state.events[conversationId]?.length ?? 0,
      lastEventSeq: state.lastEventSequences[conversationId],
      mainContent:
        state.snapshots[runtimeTargetKey(mainTarget)]?.runState?.partialContent,
      sideContent:
        state.snapshots[runtimeTargetKey(sideTarget)]?.runState?.partialContent,
    };
  });

  expect(projection).toEqual({
    eventCount: 1,
    lastEventSeq: 12,
    mainContent: "基线 + 唯一增量",
    sideContent: "基线 + 唯一增量",
  });
});