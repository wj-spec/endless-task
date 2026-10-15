/**
 * E3/B：**同一条 run 的两条 assistant 条目**必须收敛成**一个**变体。
 *
 * 硬停收尾落一条 assistant 条目；用户点「继续（接上这条运行）」原地复活（run id
 * **不变**）后再收尾，又落一条 —— 两条的 `sourceRunId` 相同、还在同一条 lane 链上，
 * 于是都会进同一个 turn 的 `draft.variants`。
 *
 * 旧写法直接 `push` 的后果（本文件就是它的反向守卫）：
 *   ① 两个变体的 `variant.id` 都是 `sourceRunId` ⇒ id 撞车（React key / 变体切换器
 *      无法区分两条）；
 *   ② `activeResponseVariantId = sourceRunId` 的 `find` 命中**第一条** ⇒ 用户看到的
 *      是硬停那一刻的旧正文，**看不到续跑出来的新正文**（复活在界面上"没有发生"）。
 *
 * 语义上"这条 run 现在的结果"只有一条 ⇒ 后续条目**顶掉**前一条。内容不会丢：复活后
 * 的收尾正文由执行器把前任各轮助手文本重新拼进去（`runner.py` 的
 * `content_parts.extend(...)`），所以最新一条就是累加后的全量。
 */
import { describe, expect, it } from "vitest";
import type {
  ConversationSnapshot,
  RuntimeV2Entry,
  RuntimeV2Snapshot,
} from "./apiTypes";
import { runtimeSnapshotToConversationSnapshot } from "./conversationSnapshotMapper";

const legacy = {
  conversation: {
    id: "conv_1",
    title: "会话",
    status: "active",
    nextTurnOrdinal: 2,
    titleIsManual: false,
    createdAt: "2026-09-15T10:00:00Z",
    updatedAt: "2026-09-15T10:00:00Z",
    archivedAt: null,
    parentConversationId: null,
    forkTurnId: null,
    kind: "normal",
    providerProfileId: null,
    modelOverride: null,
    promotedAt: null,
    workspaceId: null,
  },
  turns: [],
  files: [],
} as unknown as ConversationSnapshot;

const entry = (
  id: string,
  type: string,
  content: string,
  sourceRunId: string | null,
  seq: number,
): RuntimeV2Entry =>
  ({
    id,
    type,
    actor: type === "user_message" ? "user" : "assistant",
    status: "final",
    seq,
    createdAt: "2026-09-15T10:00:00Z",
    sourceRunId,
    data: { content },
  }) as unknown as RuntimeV2Entry;

const snapshotWith = (entries: RuntimeV2Entry[]): RuntimeV2Snapshot =>
  ({
    snapshotVersion: 1,
    conversationId: "conv_1",
    activeLaneId: "lane_1",
    mainLaneId: "lane_1",
    runningLaneId: null,
    runningRunId: null,
    activeRunId: "run_1",
    activeRunVariantId: "run_1",
    revivableRunId: null,
    lastEventSeq: 0,
    entries,
    runState: null,
    pendingApprovals: [],
    toolStates: [],
    contextUsage: { inputTokens: 0, outputTokens: 0 },
    interruptedRuns: [],
    stuck: null,
    escalation: null,
    verification: null,
    usage: null,
    capabilities: [],
  }) as unknown as RuntimeV2Snapshot;

describe("runtimeSnapshotToConversationSnapshot（E3/B 同 run 的第二条 assistant 条目）", () => {
  it("复活后同 run 的两条 assistant 条目收敛成一个变体，且显示**最新**那条", () => {
    const mapped = runtimeSnapshotToConversationSnapshot(
      legacy,
      snapshotWith([
        entry("entry_user", "user_message", "读取文件", null, 1),
        entry(
          "entry_stop",
          "assistant_message",
          "我先读取文件。\n\n[时间到墙，已安全停止]",
          "run_1",
          2,
        ),
        entry(
          "entry_resumed",
          "assistant_message",
          "我先读取文件。文件内容是 file content。",
          "run_1",
          3,
        ),
      ]),
    );

    expect(mapped.turns).toHaveLength(1);
    const turn = mapped.turns[0];
    // ① 一个变体（不是一个 run 两条同 id 的变体）。
    expect(turn.responseVariants).toHaveLength(1);
    expect(turn.responseVariants[0].variant.id).toBe("run_1");
    // ② 显示的是**续跑后**的那条正文（旧写法命中的是硬停那条 ⇒ 这里会红）。
    expect(
      turn.responseVariants[0].assistantMessage.content,
    ).toContain("文件内容是 file content。");
    expect(turn.activeResponseVariantId).toBe("run_1");
    expect(turn.turn.activeResponseVariantId).toBe("run_1");
  });

  it("反向：**不同** run 的 assistant 条目仍然各自成一个变体（重生成/编辑重跑语义不动）", () => {
    const mapped = runtimeSnapshotToConversationSnapshot(
      legacy,
      snapshotWith([
        entry("entry_user", "user_message", "改写一句话", null, 1),
        entry("entry_a", "assistant_message", "回答 A", "run_a", 2),
        entry("entry_b", "assistant_message", "回答 B", "run_b", 3),
      ]),
    );

    expect(mapped.turns).toHaveLength(1);
    expect(
      mapped.turns[0].responseVariants.map((item) => item.variant.id),
    ).toEqual(["run_a", "run_b"]);
    // 活动变体由会话指针（activeRunVariantId=run_a）决定 —— 既有口径不变。
    expect(mapped.turns[0].activeResponseVariantId).toBe("run_a");
  });
});
