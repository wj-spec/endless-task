import { expect, test } from "@playwright/test";
import {
  apiUrl,
  createConversation,
  openConversation,
  runtimeSnapshot,
} from "../support/api";

/**
 * E3/B：**预算硬停**后的 run 上点「继续（接上这条运行）」⇒ 在**同一条 run** 上接着跑。
 *
 * 走的是用户能看到的整条链：硬停横幅（`run.awaiting_user` willStop + `run.finished`）
 * → 后端判据 `snapshot.revivableRunId` → 按钮 → `POST /api/v2/runs/{id}/recovery
 * {"action":"resume"}` → 同 run 复活 + 启动 → 同一条 run 收尾。
 *
 * ⚠️ **硬停那一格必须在打开会话之前注入**（实测口径，不是偏好）：控制器的实时通道
 * `runtimeController.followConversation` 在"这条会话当前没有在飞 run"时
 * **拉一次快照就结束**（`if (!snapshot.runningRunId) break`）⇒ 空转会话页面上不会
 * 有长连 SSE，**事后**追加的事件送不到前端。生产里硬停发生在 run 还在飞的时候
 * （流是开着的），所以那不是产品行为差异；但这条 e2e 只能走"进页面时取到的那份
 * 快照"这条路（`revivableRunId` 就是快照字段，与前端 `runningRunId` 同一种姿势）。
 *
 * 硬停那一格由 `__e2e/fixtures/hard-stopped-run` 按生产顺序如实造出（生产里它由
 * `_finalize_time_budget` 落下，而 AppSettings 的时间墙是**全局**设置，调小会波及别的 spec）。
 *
 * 四条断言都是**用户可感知**的：
 *   ① 点下去以后**没有新开一轮**（没有新增用户消息、run id 不变）；
 *   ② 出现了续跑出来的新正文（真的接着跑，而不是把旧结果再显示一遍）；
 *   ③ 横幅过期（同 run 的恢复事件 → 升级投影消失）；
 *   ④ 模型轮接了前任（1 → 2），即前任那一轮**没有被重做**。
 */
test("硬停后的 run：点「继续（接上这条运行）」在**同一条 run** 上接着跑", async ({
  page,
  request,
}) => {
  const title = `E2E 硬停续跑 ${Date.now()}`;
  const conversation = await createConversation(request, title);

  const seeded = await request.post(
    `${apiUrl}/__e2e/fixtures/hard-stopped-run`,
    { data: { conversationId: conversation.id } },
  );
  expect(seeded.ok(), await seeded.text()).toBeTruthy();
  const { runId, laneId } = (await seeded.json()) as {
    runId: string;
    laneId: string;
  };

  // 前置事实（后端口径）：硬停形态 + 后端判它**可复活**。
  const before = await runtimeSnapshot(request, conversation.id, laneId);
  expect(before.runState?.status).toBe("completed");
  expect(before.escalation?.willStop).toBe(true);
  expect(before.revivableRunId).toBe(runId);

  await openConversation(page, title);

  const resumeButton = page.getByRole("button", {
    name: "继续（接上这条运行）",
  });
  await expect(resumeButton).toBeVisible();
  await expect(page.getByText("我先读取工作区文件。")).toBeVisible();

  await resumeButton.click();

  // ② 续跑出来的新正文出现（硬停前那条正文与它是**不同**的两句）。
  await expect(page.getByText("（复活后接着跑）E2E 续跑回复")).toBeVisible({
    timeout: 15_000,
  });
  // ③ 横幅过期：同 run 的恢复事件让这条"已安全停止，等你决定"不再挂着。
  await expect(
    page.getByRole("button", { name: "继续（接上这条运行）" }),
  ).toHaveCount(0);

  const after = await runtimeSnapshot(request, conversation.id, laneId);
  // ④ **同一条 run**：run id 不变、状态收尾、模型轮接了前任（1 → 2，前任没被重做）。
  expect(after.runState?.runId).toBe(runId);
  expect(after.runState?.status).toBe("completed");
  expect(after.runState?.modelTurns.length).toBe(2);
  expect(after.revivableRunId ?? null).toBeNull();
  expect(after.escalation ?? null).toBeNull();
  // ⑤ 没有新开一轮：transcript 里**只有触发的那一条**用户消息（复活不新增用户消息）。
  expect(
    after.entries.filter((entry) => entry.type === "user_message"),
  ).toHaveLength(1);
});
