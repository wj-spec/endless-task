import { useState } from "react";
import { ChatWorkSurface } from "./features/chat/ChatWorkSurface";
import { SessionRail } from "./features/chat/SessionRail";
import { useChatApplication } from "./features/chat/useChatApplication";

export function App() {
  const chat = useChatApplication();
  const [railOpen, setRailOpen] = useState(false);

  return (
    <div className="app-shell">
      <SessionRail
        activeConversationId={chat.activeConversationId}
        conversations={chat.conversations}
        health={chat.health}
        open={railOpen}
        search={chat.search}
        statusFilter={chat.statusFilter}
        onClose={() => setRailOpen(false)}
        onNewConversation={() => {
          void chat.newConversation();
          setRailOpen(false);
        }}
        onSearchChange={chat.setSearch}
        onSelectConversation={(conversationId) => {
          void chat.openConversation(conversationId);
          setRailOpen(false);
        }}
        onStatusFilterChange={chat.setStatusFilter}
      />
      <ChatWorkSurface
        conversation={chat.activeSnapshot}
        draft={chat.draft}
        error={chat.error}
        health={chat.health}
        isGenerating={chat.isGenerating}
        liveTurns={chat.liveTurns}
        loading={chat.loading}
        pendingAction={chat.pendingAction}
        onArchive={() => void chat.changeConversationStatus("archived")}
        onCancel={() => void chat.cancel()}
        onDelete={() => void chat.deleteConversation()}
        onDismissError={() => chat.setError(null)}
        onDraftChange={chat.setDraft}
        onMenu={() => setRailOpen(true)}
        onRegenerate={(turnId) => void chat.regenerate(turnId)}
        onResolveApproval={(turnId, approvalId, decision) =>
          void chat.resolveApproval(turnId, approvalId, decision)
        }
        onRemoveFile={(fileId) => void chat.removeFile(fileId)}
        onRename={(title) => void chat.renameConversation(title)}
        onRestore={() => void chat.changeConversationStatus("active")}
        onRetry={(turnId) => void chat.retry(turnId)}
        onSelectVariant={(turnId, variantId) =>
          void chat.selectVariant(turnId, variantId)
        }
        onSend={() => void chat.send()}
        onUploadFile={(file) => void chat.uploadFile(file)}
      />
      {railOpen ? (
        <button
          aria-label="关闭会话列表"
          className="rail-scrim"
          onClick={() => setRailOpen(false)}
          type="button"
        />
      ) : null}
    </div>
  );
}
