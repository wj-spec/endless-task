import type {
  ConversationSnapshot,
  Message,
  RuntimeV2Entry,
  RuntimeV2Snapshot,
  TurnStatus,
} from "./apiTypes";

// P3-3 split slice A: pure v2 snapshot -> conversation snapshot mapper.
// Relocated verbatim from useChatApplication.ts; no behavior change.

export const runtimeStatusToTurnStatus = (status: string): TurnStatus =>
  status === "completed" ||
  status === "failed" ||
  status === "cancelled" ||
  status === "running"
    ? status
    : "running";

const runtimeEntryStatusToTurnStatus = (status: string): TurnStatus =>
  status === "failed" || status === "cancelled" ? status : "completed";

const entryMessage = (
  entry: RuntimeV2Entry,
  role: "user" | "assistant",
  turnId: string,
): Message => ({
  id: entry.id,
  conversationId: "",
  turnId,
  role,
  content: entry.data.content ?? "",
  createdAt: entry.createdAt,
  updatedAt: entry.createdAt,
});


export const runtimeSnapshotToConversationSnapshot = (
  legacy: ConversationSnapshot,
  runtime: RuntimeV2Snapshot,
): ConversationSnapshot => {
  const turns: ConversationSnapshot["turns"] = [];
  let draft: {
    user: RuntimeV2Entry;
    variants: RuntimeV2Entry[];
  } | null = null;

  const flushDraft = (status: TurnStatus = "completed") => {
    if (!draft) return;
    const currentDraft = draft;
    const firstAssistant = currentDraft.variants[0];
    const turnId = firstAssistant
      ? (firstAssistant.sourceRunId ?? `${currentDraft.user.id}:turn`)
      : (runtime.activeRunId ?? `${currentDraft.user.id}:turn`);
    // 活动变体优先：conversation 指针指向的 run（重生成/编辑后的新回答）优先展示
    const activeVariantId =
      currentDraft.variants.find(
        (item) => item.sourceRunId === runtime.activeRunVariantId,
      )?.sourceRunId ??
      firstAssistant?.sourceRunId ??
      turnId;
    const variantStatus = firstAssistant
      ? runtimeEntryStatusToTurnStatus(firstAssistant.status)
      : status;
    turns.push({
      turn: {
        id: turnId,
        conversationId: legacy.conversation.id,
        ordinal: turns.length + 1,
        userMessageId: currentDraft.user.id,
        activeResponseVariantId: activeVariantId,
        status: variantStatus,
        createdAt: currentDraft.user.createdAt,
        startedAt: currentDraft.user.createdAt,
        finishedAt: firstAssistant?.createdAt ?? null,
        inherited: currentDraft.user.inherited === true,
      },
      userMessage: entryMessage(currentDraft.user, "user", turnId),
      activeResponseVariantId: activeVariantId,
      responseVariants: currentDraft.variants.map((assistant, index) => ({
        variant: {
          id: assistant.sourceRunId ?? `${turnId}:variant:${index}`,
          turnId,
          assistantMessageId: assistant.id,
          index,
          operation: "create",
          status: runtimeEntryStatusToTurnStatus(assistant.status),
          provider: null,
          model: null,
          finishReason:
            assistant.status === "cancelled"
              ? "cancelled"
              : assistant.status === "failed"
                ? "error"
                : "stop",
          errorCode: null,
          inputTokens: null,
          outputTokens: null,
          createdAt: assistant.createdAt,
          startedAt: null,
          finishedAt: assistant.createdAt,
        },
        assistantMessage: entryMessage(assistant, "assistant", turnId),
      })),
      activities: [],
    });
    if (!currentDraft.variants.length && status !== "completed") {
      turns.at(-1)!.responseVariants.push({
        variant: {
          id: turnId,
          turnId,
          assistantMessageId: `${turnId}:pending`,
          index: 0,
          operation: "create",
          status,
          provider: null,
          model: null,
          finishReason: null,
          errorCode: null,
          inputTokens: null,
          outputTokens: null,
          createdAt: currentDraft.user.createdAt,
          startedAt: currentDraft.user.createdAt,
          finishedAt: null,
        },
        assistantMessage: {
          id: `${turnId}:pending`,
          conversationId: legacy.conversation.id,
          turnId,
          role: "assistant",
          content: "",
          createdAt: currentDraft.user.createdAt,
          updatedAt: currentDraft.user.createdAt,
        },
      });
    }
    draft = null;
  };

  for (const entry of runtime.entries) {
    if (entry.type === "user_message") {
      flushDraft();
      draft = { user: entry, variants: [] };
      continue;
    }
    if (entry.type === "assistant_message" && draft) {
      // E3/B：**同一条 run 可以有两条 assistant 条目** —— 硬停收尾落一条，用户点
      // 「继续（接上这条运行）」原地复活后收尾又落一条（`revive_terminal_run` 不新建
      // run ⇒ 两条的 `sourceRunId` 相同、还在同一条 lane 链上）。旧写法会把它们当
      // **两个变体**，而 `variant.id = sourceRunId` ⇒ 两个变体同 id（React key 撞车 +
      // 变体切换器无法区分），且 `find(activeResponseVariantId)` 命中的是**第一条**
      // （用户看不到续跑出来的那段）。
      // ⇒ 同一 run 的后续 assistant 条目**顶掉**前一条：语义就是"这条 run 现在的结果"。
      // 内容不会丢：复活后的收尾正文由执行器把前任各轮的助手文本重新拼进去
      // （`runner.py` 的 `content_parts.extend(...)`），所以最新一条就是累加后的全量。
      const sourceRunId = entry.sourceRunId;
      const previousIndex =
        sourceRunId === undefined
          ? -1
          : draft.variants.findIndex((item) => item.sourceRunId === sourceRunId);
      if (previousIndex >= 0) draft.variants[previousIndex] = entry;
      else draft.variants.push(entry);
    }
  }

  if (draft) {
    flushDraft(
      runtime.runState ? runtimeStatusToTurnStatus(runtime.runState.status) : "completed",
    );
  }

  return {
    ...legacy,
    conversation: {
      ...legacy.conversation,
      nextTurnOrdinal: turns.length + 1,
    },
    turns,
  };
};

