import type { Conversation } from "./apiTypes";
import type { SessionActions } from "./workspaceNavigationModel";
import { RECENT_VISIBLE_SESSIONS } from "./workspaceNavigationModel";
import { WorkspaceSessionList } from "./WorkspaceSessionList";

type RecentSessionGroupProps = {
  conversations: Conversation[];
  activeConversationId: string | null;
  showAll: boolean;
  renamingId: string | null;
  renameDraft: string;
  onToggleShowAll: () => void;
  actions: SessionActions;
};

export function RecentSessionGroup({
  conversations,
  activeConversationId,
  showAll,
  renamingId,
  renameDraft,
  onToggleShowAll,
  actions,
}: RecentSessionGroupProps) {
  if (conversations.length === 0) return null;
  return (
    <section aria-label="未绑定" className="recent-group">
      <div className="recent-group-header">
        <span>未绑定</span>
      </div>
      <WorkspaceSessionList
        actions={actions}
        activeConversationId={activeConversationId}
        conversations={conversations}
        emptyDescription="未归属到工作区的对话会显示在这里；请为其绑定工作区目录后继续使用。"
        emptyTitle="还没有未绑定对话"
        listClassName="recent-session-list"
        onToggleShowAll={onToggleShowAll}
        renameDraft={renameDraft}
        renamingId={renamingId}
        showAll={showAll}
        visibleCount={RECENT_VISIBLE_SESSIONS}
      />
    </section>
  );
}