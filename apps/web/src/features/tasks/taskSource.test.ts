import { describe, expect, it } from "vitest";
import type { RuntimeV2Snapshot } from "../chat/apiTypes";
import { resolveTaskSource } from "./taskSource";

/**
 * 切片 10 T6（验收 ④）：创建定时任务的来源锚点**取自当前会话的当前轮次**。
 *
 * 后端 `POST /tasks` 的 `conversationId`/`turnId` 必填（`tasks.source_conversation_id`
 * 被 worker 当 EPHEMERAL 子会话的父会话用，`source_turn_id` 是 `NOT NULL`），所以
 * "拿不到就不建"必须是一条**可验证**的规则，而不是靠调用方自觉。
 */
const snapshotWith = (state: Record<string, unknown>): RuntimeV2Snapshot =>
  state as unknown as RuntimeV2Snapshot;

describe("resolveTaskSource：来源锚点 = 当前会话 + 当前轮次", () => {
  it("当前会话的快照：取 activeRunId（= 该车道当前轮次的 v2 run id）", () => {
    expect(
      resolveTaskSource(
        "conv_1",
        snapshotWith({ conversationId: "conv_1", activeRunId: "run_9" }),
      ),
    ).toEqual({ conversationId: "conv_1", turnId: "run_9" });
  });

  it("快照不是这个会话的 → null（不许把别的会话的轮次当来源）", () => {
    expect(
      resolveTaskSource(
        "conv_1",
        snapshotWith({ conversationId: "conv_other", activeRunId: "run_9" }),
      ),
    ).toBeNull();
  });

  it("会话里还没有任何一轮（activeRunId 为空）→ null（不编 id）", () => {
    expect(
      resolveTaskSource(
        "conv_1",
        snapshotWith({ conversationId: "conv_1", activeRunId: null }),
      ),
    ).toBeNull();
  });

  it("没有当前会话 / 没有快照 → null", () => {
    expect(resolveTaskSource(null, snapshotWith({ conversationId: "c" }))).toBeNull();
    expect(resolveTaskSource("conv_1", null)).toBeNull();
  });

  it("别的车道正在跑时，仍然取**当前车道当前轮**（runningRunId 是会话级的，会串车道）", () => {
    expect(
      resolveTaskSource(
        "conv_1",
        snapshotWith({
          conversationId: "conv_1",
          activeLaneId: "lane_main",
          activeRunId: "run_main_7",
          runningLaneId: "lane_other",
          runningRunId: "run_other_lane",
        }),
      ),
    ).toEqual({ conversationId: "conv_1", turnId: "run_main_7" });
  });
});
