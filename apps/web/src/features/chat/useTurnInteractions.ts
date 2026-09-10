import { useCallback, useEffect, useState } from "react";
import { chatApi } from "./api";
import type { KnowledgeCitation } from "./apiTypes";
import type { WorkspacePreviewTarget } from "./WorkspaceFilePreviewDrawer";

/**
 * 工作面上的"逐轮交互态"：消息编辑、审批改参、引用卡片、文件预览。
 *
 * 从 `ChatWorkSurface.tsx`（1 439 行）里收拢出来。这四处状态原本散在组件的
 * 330–510 行之间（`editTurnId`/`editDraft`、`modifyApprovalId`/`modifyDraft`/
 * `modifyError`、`citationsByTurn`/`openCitation`、`previewTarget`），而它们的使用点
 * 全在**轮次渲染块**里——收进一个 hook 后，下一轮把轮次块拆成 `TurnItem` 时可以直接
 * 把这一整个对象传下去，不必再逐个透传十几个 setState。
 *
 * 行为零改动：状态初值与 setter 调用点逐一对应搬移；引用列表的拉取 effect
 * （`conversationId` → `getConversationCitations`）原样搬入，失败静默的兜底保持不变。
 */
export type TurnInteractions = {
  /** 正在编辑的用户消息轮次 id（null = 没有在编辑）。 */
  editTurnId: string | null;
  setEditTurnId: (turnId: string | null) => void;
  /** 编辑中的正文；打开编辑器时由调用方填入原消息。 */
  editDraft: string;
  setEditDraft: (value: string) => void;

  /** 正在改参数的审批 id（null = 没有在改）。 */
  modifyApprovalId: string | null;
  setModifyApprovalId: (approvalId: string | null) => void;
  modifyDraft: string;
  setModifyDraft: (value: string) => void;
  /** 改参的校验错误（JSON 解析失败等）。 */
  modifyError: string | null;
  setModifyError: (value: string | null) => void;

  /** 会话「真正注入」的引用：turn_id → 引用列表（用于区分真实引用与占位角标）。 */
  citationsByTurn: Record<string, KnowledgeCitation[]>;
  /** 当前展开的引用卡片（同一轮次同一标签再点一次即收起）。 */
  openCitation: { turnId: string; label: string } | null;
  setOpenCitation: (value: { turnId: string; label: string } | null) => void;
  /** 点击引用角标：记录点击（失败静默）并切换展开态。 */
  handleCitationClick: (turnId: string, label: string) => Promise<void>;

  /** 正在预览的工作区文件（引用/来源点击后打开抽屉）。 */
  previewTarget: WorkspacePreviewTarget | null;
  setPreviewTarget: (value: WorkspacePreviewTarget | null) => void;

  /** 关闭所有"逐轮交互"面板（切换会话时调用，避免残留上一会话的卡片）。 */
  resetForConversation: () => void;
};

export function useTurnInteractions(
  conversationId: string | undefined,
): TurnInteractions {
  const [editTurnId, setEditTurnId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState("");
  const [modifyApprovalId, setModifyApprovalId] = useState<string | null>(null);
  const [modifyDraft, setModifyDraft] = useState("");
  const [modifyError, setModifyError] = useState<string | null>(null);
  const [citationsByTurn, setCitationsByTurn] = useState<
    Record<string, KnowledgeCitation[]>
  >({});
  const [openCitation, setOpenCitation] = useState<{
    turnId: string;
    label: string;
  } | null>(null);
  const [previewTarget, setPreviewTarget] =
    useState<WorkspacePreviewTarget | null>(null);

  // 拉取本轮会话「真正注入」的引用列表（turn_id -> citations）。
  // 前端据此把 [K1]/[K2]… 角标分为「真实引用」与「无法溯源的占位引用」。
  useEffect(() => {
    if (!conversationId) {
      setCitationsByTurn({});
      return;
    }
    let cancelled = false;
    void chatApi
      .getConversationCitations(conversationId)
      .then((map) => {
        if (!cancelled) setCitationsByTurn(map);
      })
      .catch(() => {
        // 引用加载失败不阻断交互（此时角标保持乐观可点击）。
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  const handleCitationClick = useCallback(
    async (turnId: string, label: string) => {
      // 与搬迁前逐行等价：同一个（轮次,标签）再点一次 = 收起；否则展开；
      // 命中真实引用时才上报点击（失败静默）。
      let toggledOff = false;
      setOpenCitation((current) => {
        if (current && current.turnId === turnId && current.label === label) {
          toggledOff = true;
          return null;
        }
        return { turnId, label };
      });
      if (toggledOff) return;
      const items = citationsByTurn[turnId] ?? [];
      const citation = items.find((item) => item.label === label);
      if (citation) {
        void chatApi
          .recordCitationClick({
            label: citation.label,
            scope: citation.scope,
            refId: citation.refId,
            turnId,
            conversationId: citation.conversationId,
          })
          .catch(() => undefined);
      }
    },
    [citationsByTurn],
  );

  const resetForConversation = useCallback(() => {
    setOpenCitation(null);
  }, []);

  return {
    editTurnId,
    setEditTurnId,
    editDraft,
    setEditDraft,
    modifyApprovalId,
    setModifyApprovalId,
    modifyDraft,
    setModifyDraft,
    modifyError,
    setModifyError,
    citationsByTurn,
    openCitation,
    setOpenCitation,
    handleCitationClick,
    previewTarget,
    setPreviewTarget,
    resetForConversation,
  };
}
