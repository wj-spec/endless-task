import { useId, useMemo } from "react";
import type { Conversation, RuntimeV2Lane } from "../../chat/apiTypes";
import { orderLanes } from "../../chat/BranchNavigator";
import { friendlyLaneName } from "../../chat/laneLabel";
import { conversationTitle } from "../../chat/workspaceNavigationModel";
import { AddIcon } from "../../ui/Icons";

export type ConversationsViewProps = {
  /** 当前会话的分支树（`laneTrees[activeConversationId]`，**已在内存里**）。 */
  lanes: RuntimeV2Lane[];
  /** 当前正在看的那条 lane（高亮用）。 */
  currentLaneId: string | null;
  /** 既有会话列表（与左栏同源）；这里只挑 `kind === "ephemeral"` 的。 */
  conversations: Conversation[];
  /** 「新建」：**复用既有创建流程**（`chat.createTemporaryConversation`），仍开在主区并排分栏。 */
  onCreateTemporaryConversation: () => void;
  /** 打开某条临时对话（与左栏点击同一动作）。 */
  onOpenConversation: (conversationId: string) => void;
  /** 切到某条分支（与分支导航器「切换」同一动作）。 */
  onSelectLane: (laneId: string) => void;
};

const isArchivedLane = (lane: RuntimeV2Lane) =>
  lane.status === "archived" || lane.archived;

const laneLabel = (lane: RuntimeV2Lane) =>
  friendlyLaneName(lane, lane.isMain ? "当前主线" : "未命名分支");

const temporaryStatus = (conversation: Conversation) =>
  conversation.status === "archived" ? "已归档" : "进行中";

/**
 * C4 / R-C3「解释 A」：右侧边栏的「会话」视图。
 *
 * 三件事，**都不新开请求**：
 * 1. 当前会话的分支树 —— 数据来自 `laneTrees`（已随工作面加载，排序复用
 *    `BranchNavigator` 的 `orderLanes`，不另造一套）；
 * 2. 临时对话 —— 复用既有会话列表，按 `kind === "ephemeral"` 判据挑；**判据只有
 *    `kind`**，不做"进行中"过滤（已归档的照样列出来，只是每行标签写「已归档」）；
 *    因此小节标题/空态**不带"进行中"**（C4 复审判 I-3：带上就会在左栏切到
 *    「已归档」时出现"标题写进行中、行标写已归档"的自相矛盾）；
 * 3. 「新建」—— 交给 App 级回调，**与既有创建入口同一条路**（仍开在主区并排分栏）。
 *
 * 明确**不做**"解释 B"：不在 440px 的面板里内嵌 `ChatWorkSurface`（会变成第二份
 * 流式/审批/状态，概念也错位）。
 */
export function ConversationsView({
  lanes,
  currentLaneId,
  conversations,
  onCreateTemporaryConversation,
  onOpenConversation,
  onSelectLane,
}: ConversationsViewProps) {
  const treeHeadingId = useId();
  const temporaryHeadingId = useId();
  const rows = useMemo(() => orderLanes(lanes), [lanes]);
  const temporary = useMemo(
    () => conversations.filter((item) => item.kind === "ephemeral"),
    [conversations],
  );

  return (
    <div className="workspace-conversations">
      <button
        className="workspace-temporary-new"
        onClick={onCreateTemporaryConversation}
        type="button"
      >
        <AddIcon size={13} />
        新建临时对话
      </button>
      <section aria-labelledby={treeHeadingId} className="workspace-section">
        <h3 className="workspace-section-title" id={treeHeadingId}>
          当前会话的分支树
        </h3>
        {rows.length === 0 ? (
          <p className="workspace-empty">
            当前会话还没有分支，从某条回答上创建分支后会出现在这里。
          </p>
        ) : (
          <ul className="workspace-lane-tree">
            {rows.map(({ lane, depth }) => {
              const current = lane.id === currentLaneId;
              return (
                <li
                  key={lane.id}
                  style={{ paddingInlineStart: `${depth * 14}px` }}
                >
                  <button
                    aria-current={current ? "true" : undefined}
                    className={
                      current
                        ? "workspace-lane-row is-current"
                        : "workspace-lane-row"
                    }
                    onClick={() => onSelectLane(lane.id)}
                    type="button"
                  >
                    <span className="workspace-item-title">
                      {laneLabel(lane)}
                    </span>
                    <span className="workspace-item-meta">
                      {lane.isMain ? "主线" : "分支"}
                      {isArchivedLane(lane) ? " · 已归档" : ""}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </section>
      <section aria-labelledby={temporaryHeadingId} className="workspace-section">
        <h3 className="workspace-section-title" id={temporaryHeadingId}>
          临时对话
        </h3>
        {temporary.length === 0 ? (
          <p className="workspace-empty">当前没有临时对话。</p>
        ) : (
          <ul className="workspace-temporary-list">
            {temporary.map((item) => (
              <li key={item.id}>
                <button
                  onClick={() => onOpenConversation(item.id)}
                  type="button"
                >
                  <span className="workspace-item-title">
                    {conversationTitle(item)}
                  </span>
                  <span className="workspace-item-meta">
                    {temporaryStatus(item)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
