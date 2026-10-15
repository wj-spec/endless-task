import type { RuntimeV2Snapshot } from "../chat/apiTypes";

/**
 * 切片 10 T6：创建定时任务需要的**来源锚点**。
 *
 * 后端 `POST /tasks` 把 `conversationId` / `turnId` 定为必填（`missing_source_conversation`
 * / `missing_source_turn`）：`tasks.source_conversation_id` 被 worker 当作 EPHEMERAL
 * 执行子会话的**父会话**，`source_turn_id` 是 `NOT NULL` 的来源轮次（迁移 016）。
 * 所以"凭空建任务"在这条路上不存在——必须绑定**当前会话的当前轮次**。
 *
 * 来源不是猜的，也不是硬编码：
 *
 * - `conversationId`：就是面板拿到的当前会话（`App.tsx` → `chat.activeConversationId`
 *   → `AssistantPanel.conversationId`）。
 * - `turnId`：本项目里 v2 的 turnId **就是那一轮的 run id**，三处同一口径——
 *   `TaskNotification.turnId` 的注释、`App.tsx:300` 通知定位、以及
 *   `chatApplicationSupport.liveFromRuntimeSnapshot`（`turnId: run.runId`）。
 *   快照字段 `activeRunId` 的语义正好是"**当前车道的当前轮次**"：
 *   `runtime_v2/replay.py:325-334` 取的是该 lane 里 `is_active_variant` 的那条 run，
 *   而 `is_active_variant=True` 在 run **创建时**就写入（`runtime_v2/gateway_impl/runs.py:239`）、
 *   终态时保留（`storage/v2/runs.py:530`）——所以在飞指向在飞 run、跑完仍指向那一轮。
 *   这里**刻意不用** `runningRunId` 兜底：它是**会话级**的（`replay.py:340-344` 收全部
 *   车道里在跑的 run），拿它当"当前轮次"会把别的车道/别的 run 混进来。
 *
 * 拿不到（没有会话、快照不属于这个会话、该会话还没有任何一轮）时返回 `null`——
 * 面板据此**禁止提交**，绝不编一个 id 出来（见 `ScheduledTasks.tsx` 的表单）。
 */
export type TaskSource = {
  conversationId: string;
  turnId: string;
};

export const resolveTaskSource = (
  conversationId: string | null,
  runtimeSnapshot: RuntimeV2Snapshot | null,
): TaskSource | null => {
  if (!conversationId) return null;
  if (!runtimeSnapshot) return null;
  // 快照必须属于**这个**会话：控制器按会话/车道分桶，但面板不该假设调用方一定传对了。
  if (runtimeSnapshot.conversationId !== conversationId) return null;
  const turnId = runtimeSnapshot.activeRunId;
  if (!turnId) return null;
  return { conversationId, turnId };
};
