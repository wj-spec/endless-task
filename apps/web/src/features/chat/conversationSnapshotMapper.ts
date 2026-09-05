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
      draft.variants.push(entry);
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

