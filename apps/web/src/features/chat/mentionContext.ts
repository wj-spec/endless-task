/**
 * C3b：`@` 引用会话上下文的纯逻辑（候选筛选、档位、与 `/` 浮层共用的窗口算术）。
 *
 * 这里刻意**不碰 DOM、不发请求**：所有需要下断言的口径（哪些会话可引用、引用项
 * 长什么样、预览失败算不算"拦发送"）都收在这一层，组件只负责画。
 */
import type { Conversation, RuntimeV2ConversationReference } from "./apiTypes";

/** 一期 UI 只给两档：`summary`（默认）与 `recent`（`full` 见 R-C4，见下）。 */
export const DEFAULT_RECENT_TURNS = 5;

/**
 * `recent` 档可选的轮数——**与后端 `RECENT_MODE_TURN_CHOICES` 同值**。
 *
 * 后端 `mentions/service.py` 对越界值指名 400，所以这里不是"UI 偏好"而是契约；
 * 改一处必须两处一起改（同 `/技能名` 与技能清单的做法）。
 */
export const RECENT_TURN_CHOICES: readonly number[] = [5, 10];

/**
 * 单轮默认引用条数（R-C4 / `02 §3.1`：第 3 条起只**告警**不拒绝，所以不是上限）。
 *
 * 后端的真实上限是"单轮总量预算 ≈4096 token"与"单条档位上限"，都会体现在
 * 预览返回的 `warnings` 里——前端**不自己算 token**，只用这个数字做一句提示。
 */
export const DEFAULT_MENTION_REFERENCES = 2;

/**
 * `@` 候选的一期可引用面：**同工作区、未归档、且不是本次发送的会话**。
 *
 * 后端把这三条都当硬 400（`mention_conversation_not_found` /
 * `mention_conversation_unreadable` / `mention_self_reference`），所以候选列表先
 * 按能本地判定的两条筛掉——`archived` 与"自己引用自己"。剩余的（例如目标会话
 * 还没有 v2 lane）由预览端点指名报错，前端不复刻。
 */
export function filterMentionCandidates(
  conversations: Conversation[],
  options: { excludeConversationId: string | null; query: string },
): Conversation[] {
  const query = options.query.trim().toLocaleLowerCase();
  return conversations.filter((item) => {
    if (item.id === options.excludeConversationId) return false;
    if (item.status !== "active") return false;
    if (!query) return true;
    return item.title.toLocaleLowerCase().includes(query);
  });
}

/** 候选行的副标题：会话类型 + 最后更新日期（纯可读性，不参与任何判定）。 */
export function mentionCandidateMeta(candidate: Conversation): string {
  const kind = candidate.kind === "ephemeral" ? "临时对话" : "会话";
  const updated = candidate.updatedAt ? candidate.updatedAt.slice(0, 10) : "";
  return updated ? `${kind} · ${updated} 更新` : kind;
}

/** 档位 → `<select>` 的取值（`summary` / `recent:N`）。 */export function mentionModeValue(reference: RuntimeV2ConversationReference): string {
  if (reference.mode === "recent") {
    return `recent:${reference.recentTurns ?? DEFAULT_RECENT_TURNS}`;
  }
  return "summary";
}

/**
 * 一条引用项（判别联合的 `kind="conversation"` 成员）。
 *
 * `mode` **显式**送出去（协议默认也是 `summary`）：`entryIds` 一期预留，送空数组
 * 让后端拿到确定形状。`recent` 档必须带 `recentTurns`——协议里它可缺省，但缺省值
 * 的权威在后端，不显式给就会让"预览口径"与"发送口径"两处漂移。
 */
export function conversationReference(
  conversationId: string,
  modeValue: string = "summary",
): RuntimeV2ConversationReference {
  if (modeValue.startsWith("recent:")) {
    const parsed = Number.parseInt(modeValue.slice("recent:".length), 10);
    const recentTurns = RECENT_TURN_CHOICES.includes(parsed)
      ? parsed
      : DEFAULT_RECENT_TURNS;
    return {
      kind: "conversation",
      conversationId,
      mode: "recent",
      recentTurns,
      entryIds: [],
    };
  }
  return {
    kind: "conversation",
    conversationId,
    mode: "summary",
    entryIds: [],
  };
}

/** 已选中的一条：引用项 + 展示用的标题（标题来自候选列表，不在协议里）。 */
export type MentionSelection = {
  reference: RuntimeV2ConversationReference;
  title: string;
};

/**
 * 预览失败 → 给用户看的文案 + **要不要拦住发送**。
 *
 * 只有"后端明确拒绝"（4xx）才算拦：那是同一份校验在发送前给出的确定结论。
 * 网络/5xx 只是预览没拿到，发送本身仍可能成功，不该把功能锁死。
 */
export function previewFailure(error: unknown): {
  message: string;
  blocking: boolean;
} {
  const status =
    typeof (error as { status?: unknown } | null)?.status === "number"
      ? (error as { status: number }).status
      : 0;
  const message =
    error instanceof Error && error.message
      ? error.message
      : "引用预览失败，暂时无法确认引用大小。";
  return { message, blocking: status >= 400 && status < 500 };
}

/** `/` 与 `@` 浮层共用的窗口算术（可见切片 + 夹回下标 + 是否补计数行）。 */
export type MenuWindow = {
  /** 真正渲染出来的条数（防御性上限之内）。 */
  visibleCount: number;
  /** 因上限没渲染出来的条数。 */
  hiddenCount: number;
  /** 夹回 `[0, visibleCount)` 的高亮下标。 */
  activeIndex: number;
  /** 是否需要补一行"共 N 条"的统计提示。 */
  showCount: boolean;
};

/**
 * 两个浮层共用同一套骨架（`03`… R-C10 的"通用浮层"）：候选多到要滚动时补一行
 * 总数，键盘高亮只在**真正渲染出来的那批**上移动，列表收窄时把旧下标夹回范围。
 */
export function menuWindow(
  matchCount: number,
  activeIndex: number,
  options: { limit: number; visibleRows: number },
): MenuWindow {
  const visibleCount = Math.max(0, Math.min(matchCount, options.limit));
  const hiddenCount = Math.max(0, matchCount - visibleCount);
  const clamped =
    visibleCount > 0 ? Math.min(Math.max(0, activeIndex), visibleCount - 1) : 0;
  return {
    visibleCount,
    hiddenCount,
    activeIndex: clamped,
    showCount: visibleCount > options.visibleRows || hiddenCount > 0,
  };
}
