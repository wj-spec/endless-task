import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { createPortal } from "react-dom";
import type {
  Conversation,
  ConversationSnapshot,
  HealthSnapshot,
  ProviderProfile,
  RuntimeV2ConversationReference,
  RuntimeV2ReferencePreviewResponse,
  RuntimeV2Snapshot,
  SkillInvocationCandidate,
} from "./apiTypes";
import { chatApi } from "./api";
import { ContextBudgetMeter } from "./ContextBudgetMeter";
import { AddIcon, CloseIcon, FileIcon, SendIcon } from "../ui/Icons";
import { computeMenuPosition } from "../ui/RowMenu";
import {
  DEFAULT_MENTION_REFERENCES,
  DEFAULT_RECENT_TURNS,
  RECENT_TURN_CHOICES,
  conversationReference,
  filterMentionCandidates,
  menuWindow,
  mentionCandidateMeta,
  mentionModeValue,
  previewFailure,
  type MentionSelection,
} from "./mentionContext";

/** `/` 候选项的防御性渲染上限：菜单本身可滚动，正常数量全部展示。 */
const SLASH_MENU_LIMIT = 50;
/** 260px 菜单高度内大致能完整显示的候选行数，超过就补一行总数提示。 */
const SLASH_MENU_VISIBLE_ROWS = 8;
/** `@` 候选与 `/` 候选共用同一套窗口算术（`menuWindow`），上限各自取值。 */
const MENTION_MENU_LIMIT = 50;
const MENTION_MENU_VISIBLE_ROWS = 8;

/** 只在"行首或空白后的 /token"上触发 `/` 候选（避免路径误触发）。 */
const SLASH_TRIGGER = /(^|\s)\/([a-z0-9-]*)$/;
/** 只在"行首或空白后的 @token"上触发 `@` 候选（避免邮箱地址误触发）。 */
const MENTION_TRIGGER = /(^|\s)@([^\s@]*)$/;

type ChatComposerProps = {
  conversation: ConversationSnapshot | null;
  draft: string;
  health: HealthSnapshot | null;
  isGenerating: boolean;
  loading: boolean;
  otherLaneRunning: boolean;
  pendingAction: string | null;
  providers: ProviderProfile[];
  runningLaneLabel: string;
  sideMode: "temporary_conversation" | "branch_lane" | null;
  steerable: boolean;
  variant: "main" | "side";
  onCancel: () => void;
  onDraftChange: (value: string) => void;
  onModelChange: (
    providerProfileId: string | null,
    modelOverride: string | null,
  ) => void;
  onOpenProviders?: () => void;
  onRemoveFile: (fileId: string) => void;
  /**
   * C3b：发送时把**已选的 `@` 引用**交给调用方（它要进 `references` 请求体）。
   *
   * 引用属于**这个 composer 实例**（主区/侧区各自一份），所以只能由它自己随发送
   * 交出去——放进全局状态再按 `main`/`side` 反查会在 `branch_lane` 模式下把同一
   * 会话的两个工作面串在一起。
   *
   * 返回值（C3b 修复波 §6）：`true` = 这条消息**真的提交出去了** ⇒ composer 清空
   * chip（不清的话同一条引用会被静默带进后续**每一条**消息）；`false` / `undefined`
   * / 抛错都按"没发出去"处理 ⇒ **保留** chip，别让用户重填。
   */
  onSend: (references: RuntimeV2ConversationReference[]) => void | Promise<boolean>;
  onUploadFile: (file: File) => void;
  /** A2：上下文预算（环形指示器，输入框左下角）。 */
  contextBudget?: RuntimeV2Snapshot["contextBudget"];
  /** S1：`/技能名` 候选。 */
  skillCandidates?: SkillInvocationCandidate[];
  /**
   * C3b：本次工作面**有没有**可挂载 `@` 引用的 lane。
   *
   * `null` = **确定还没有 lane**（全新会话，后端会指名 400
   * `mention_no_lane_to_anchor`）⇒ 直接不让选；`undefined` = 未知（运行时快照还没
   * 到）⇒ 不拦，避免把功能误锁死。
   *
   * M-4 更正：这个 prop **只被当作"有没有 lane"的 null 判据**，不参与"用哪条 lane"
   * 的决策。真正的锚点 lane 由提交路径自己取 `useMessageActions.ts` 的
   * `viewLaneIds ?? mainLaneIds`（查看分支时**是分支 lane**），而这里传进来的是
   * `runtimeSnapshot.mainLaneId`（会话指针的 active lane）——两者**不必须一致**，
   * 行为上也没有问题：后端 `mention_no_lane_to_anchor` 的触发条件与本判据等价。
   */
  mentionAnchorLaneId?: string | null;
  /** S7：值变化时聚焦输入框并把光标放到末尾（注入技能后用）。 */
  focusNonce?: number;
};

export function ChatComposer({
  conversation,
  draft,
  health,
  isGenerating,
  loading,
  otherLaneRunning,
  pendingAction,
  providers,
  runningLaneLabel,
  sideMode,
  steerable,
  variant,
  onCancel,
  onDraftChange,
  onModelChange,
  onOpenProviders,
  onRemoveFile,
  onSend,
  onUploadFile,
  contextBudget,
  skillCandidates = [],
  mentionAnchorLaneId,
  focusNonce = 0,
}: ChatComposerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [slashQuery, setSlashQuery] = useState<string | null>(null);
  const [slashIndex, setSlashIndex] = useState(0);
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  const [mentionIndex, setMentionIndex] = useState(0);
  const [mentionList, setMentionList] = useState<Conversation[]>([]);
  const [mentionLoading, setMentionLoading] = useState(false);
  const [mentions, setMentions] = useState<MentionSelection[]>([]);
  const [mentionPreview, setMentionPreview] =
    useState<RuntimeV2ReferencePreviewResponse | null>(null);
  const [mentionPreviewError, setMentionPreviewError] = useState<{
    message: string;
    blocking: boolean;
  } | null>(null);

  useEffect(() => {
    if (!focusNonce) return;
    const node = composerRef.current;
    if (!node) return;
    node.focus();
    node.setSelectionRange(node.value.length, node.value.length);
  }, [focusNonce]);

  const slashMatches = useMemo(() => {
    if (slashQuery === null) return [];
    const query = slashQuery.toLowerCase();
    return skillCandidates.filter((candidate) =>
      candidate.name.toLowerCase().startsWith(query),
    );
  }, [skillCandidates, slashQuery]);

  // `/` 与 `@` 共用同一套窗口算术（R-C10 的"通用浮层"骨架）：
  // 可见切片 + 把高亮下标夹回范围内 + 是否需要补一行总数提示。
  const slashWindow = menuWindow(slashMatches.length, slashIndex, {
    limit: SLASH_MENU_LIMIT,
    visibleRows: SLASH_MENU_VISIBLE_ROWS,
  });
  const slashVisible = useMemo(
    () => slashMatches.slice(0, slashWindow.visibleCount),
    [slashMatches, slashWindow.visibleCount],
  );
  const slashActiveIndex = slashWindow.activeIndex;
  const slashHiddenCount = slashWindow.hiddenCount;
  // 列表长到需要滚动时给一行统计：只看到前几行容易误判"技能没几个"。
  const slashShowCount = slashWindow.showCount;

  const slashOpen = slashQuery !== null && slashMatches.length > 0;
  // 输入了 `/` 但没有任何候选时，也要给用户一个明确反馈（否则像"没反应"）。
  const slashEmpty = slashQuery !== null && slashMatches.length === 0;

  // C3b：`@` 引用的候选数据源。**懒加载**：只有真的敲了 `@` 才去要一次列表，
  // 且按**本会话的工作区**取（跨区引用后端指名 400）；`active` 一档是因为归档
  // 会话不可引用（后端 404）。不复用侧栏那份列表：它带搜索框的过滤，会让候选
  // 随侧栏搜索词凭空变化。
  const sendingConversation = conversation?.conversation ?? null;
  const mentionOpen = mentionQuery !== null;
  // `null` = 确定没有可挂载的 lane（全新会话）⇒ 不让选，也不发预览；
  // `undefined` = 运行时快照还没到 ⇒ 不拦。
  const mentionAnchorBlocked = mentionAnchorLaneId === null;
  const mentionBlockedReason = mentionAnchorBlocked
    ? "这个会话还没有可挂载引用的上下文：先发一条消息，再使用 @ 引用。"
    : null;

  useEffect(() => {
    if (!mentionOpen || mentionAnchorBlocked || !sendingConversation) return;
    let cancelled = false;
    setMentionLoading(true);
    void chatApi
      .listConversations("active", undefined, sendingConversation.workspaceId ?? null)
      .then((items) => {
        if (!cancelled) setMentionList(items);
      })
      .catch(() => {
        if (!cancelled) setMentionList([]);
      })
      .finally(() => {
        if (!cancelled) setMentionLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [
    mentionOpen,
    mentionAnchorBlocked,
    sendingConversation,
    sendingConversation?.workspaceId,
  ]);

  // C3b 修复波（I-2 跨会话泄漏）：`mentions` 是**本组件实例**的本地 state，而主区
  // `<ChatWorkSurface>` 在会话切换时**不重挂载**（`App.tsx` 里它没有 `key`）⇒ 不显式
  // 重置，会话 A 选的引用会被静默带进会话 B 的消息：消耗 token、把无关上下文注入本次
  // run，而用户没有任何显式动作。这里在**会话 id 变化**时清空选择（预览与预览错误一起
  // 清：留着 A 的 blocking 错误会让 B 的第一条消息发不出去）。
  const sendingConversationId = sendingConversation?.id ?? null;
  useEffect(() => {
    setMentions([]);
    setMentionPreview(null);
    setMentionPreviewError(null);
  }, [sendingConversationId]);

  const mentionMatches = useMemo(
    () =>
      filterMentionCandidates(mentionList, {
        excludeConversationId: sendingConversation?.id ?? null,
        query: mentionQuery ?? "",
      }),
    [mentionList, mentionQuery, sendingConversation?.id],
  );
  const mentionWindow = menuWindow(mentionMatches.length, mentionIndex, {
    limit: MENTION_MENU_LIMIT,
    visibleRows: MENTION_MENU_VISIBLE_ROWS,
  });
  const mentionVisible = useMemo(
    () => mentionMatches.slice(0, mentionWindow.visibleCount),
    [mentionMatches, mentionWindow.visibleCount],
  );
  const mentionActiveIndex = mentionWindow.activeIndex;
  const mentionHiddenCount = mentionWindow.hiddenCount;
  const mentionShowCount = mentionWindow.showCount;
  const mentionCandidateOpen =
    mentionQuery !== null && !mentionAnchorBlocked && mentionMatches.length > 0;
  // 敲了 `@` 就一定要有反馈：有候选给列表，没有候选/none 可挂载给明确说明。
  const mentionPanelVisible = mentionQuery !== null;

  const references = useMemo(
    () => mentions.map((item) => item.reference),
    [mentions],
  );
  const mentionSendBlocked = Boolean(mentionPreviewError?.blocking);

  // 预览：token 预估**必须来自后端**（R-C4 修订）——前端不许自己算数字。
  // 版本守卫防"先选 A 再选 B，A 的响应后到"把 B 的数字盖掉。
  const previewVersion = useRef(0);
  useEffect(() => {
    if (!sendingConversation || references.length === 0) {
      setMentionPreview(null);
      setMentionPreviewError(null);
      return;
    }
    const version = (previewVersion.current += 1);
    let cancelled = false;
    void chatApi
      .previewRuntimeV2References(sendingConversation.id, references)
      .then((response) => {
        if (cancelled || previewVersion.current !== version) return;
        setMentionPreview(response);
        setMentionPreviewError(null);
      })
      .catch((error: unknown) => {
        if (cancelled || previewVersion.current !== version) return;
        setMentionPreview(null);
        setMentionPreviewError(previewFailure(error));
      });
    return () => {
      cancelled = true;
    };
  }, [sendingConversation, references]);

  // 候选浮层 portal 到 body：`.composer` 是 `overflow: hidden`，绝对定位在
  // 输入行上方的菜单会被整块裁掉（只看得见"阴影"，看不见列表）。
  // 两个浮层互斥（一个文本末尾不可能同时是 `/x` 与 `@x`），共用同一个定位壳。
  const candidateMenuVisible = slashOpen || slashEmpty || mentionPanelVisible;
  const [candidateMenuPosition, setCandidateMenuPosition] = useState<{
    top: number;
    left: number;
    width: number;
  } | null>(null);
  const inputRowRef = useRef<HTMLDivElement>(null);
  const candidateMenuRef = useRef<HTMLDivElement | null>(null);

  const placeCandidateMenu = useCallback(() => {
    const row = inputRowRef.current;
    if (!row) return;
    const rect = row.getBoundingClientRect();
    const menu = candidateMenuRef.current;
    const { top, left } = computeMenuPosition({
      triggerRect: {
        top: rect.top,
        bottom: rect.bottom,
        left: rect.left,
        right: rect.right,
      },
      menu: {
        width: Math.min(420, rect.width),
        height: menu?.offsetHeight ?? 0,
      },
      viewport: { width: window.innerWidth, height: window.innerHeight },
      // 菜单放在输入行上方（与原来一致），但不再受祖先裁剪。
      placement: "up",
    });
    setCandidateMenuPosition({ top, left, width: Math.min(420, rect.width) });
  }, []);

  useLayoutEffect(() => {
    if (candidateMenuVisible) placeCandidateMenu();
    else setCandidateMenuPosition(null);
  }, [
    candidateMenuVisible,
    placeCandidateMenu,
    slashVisible.length,
    slashShowCount,
    mentionVisible.length,
    mentionShowCount,
  ]);

  useEffect(() => {
    if (!candidateMenuVisible) return;
    const onViewportChange = () => placeCandidateMenu();
    window.addEventListener("resize", onViewportChange);
    window.addEventListener("scroll", onViewportChange, true);
    return () => {
      window.removeEventListener("resize", onViewportChange);
      window.removeEventListener("scroll", onViewportChange, true);
    };
  }, [candidateMenuVisible, placeCandidateMenu]);

  /** 只在"行首或空白后的 /token"上触发候选（避免路径误触发）。 */
  const syncSlash = (value: string, caret: number | null) => {
    const upto = caret === null ? value.length : caret;
    const match = SLASH_TRIGGER.exec(value.slice(0, upto));
    setSlashQuery(match ? match[2] : null);
    setSlashIndex(0);
  };

  /** 只在"行首或空白后的 @token"上触发引用候选（`a@b` 这类不触发）。 */
  const syncMention = (value: string, caret: number | null) => {
    const upto = caret === null ? value.length : caret;
    const match = MENTION_TRIGGER.exec(value.slice(0, upto));
    setMentionQuery(match ? match[2] : null);
    setMentionIndex(0);
  };

  const acceptSlash = (name: string) => {
    const textarea = composerRef.current;
    const caret = textarea?.selectionStart ?? draft.length;
    const before = draft.slice(0, caret);
    const match = SLASH_TRIGGER.exec(before);
    if (!match) return;
    const replaced = `${before.slice(0, match.index)}${match[1]}/${name} `;
    const next = replaced + draft.slice(caret);
    onDraftChange(next);
    setSlashQuery(null);
    requestAnimationFrame(() => {
      const node = composerRef.current;
      if (!node) return;
      node.focus();
      node.setSelectionRange(replaced.length, replaced.length);
    });
  };

  /**
   * 选中一个候选 ⇒ 变成一条 chip，并把草稿里的 `@query` 抹掉。
   *
   * 抹掉（而不是留成文本）是有意的：引用是**结构化**的随消息发出去的上下文，
   * 草稿里再留一份 `@标题` 既可能被模型当成正文，也会和 chip 重复表达。
   */
  const acceptMention = (candidate: Conversation) => {
    const textarea = composerRef.current;
    const caret = textarea?.selectionStart ?? draft.length;
    const before = draft.slice(0, caret);
    const match = MENTION_TRIGGER.exec(before);
    if (!match) return;
    const replaced = `${before.slice(0, match.index)}${match[1]}`;
    onDraftChange(replaced + draft.slice(caret));
    setMentionQuery(null);
    setMentions((current) =>
      current.some((item) => item.reference.conversationId === candidate.id)
        ? current
        : [
            ...current,
            { reference: conversationReference(candidate.id), title: candidate.title },
          ],
    );
    requestAnimationFrame(() => {
      const node = composerRef.current;
      if (!node) return;
      node.focus();
      node.setSelectionRange(replaced.length, replaced.length);
    });
  };

  const removeMention = (conversationId: string) =>
    setMentions((current) =>
      current.filter((item) => item.reference.conversationId !== conversationId),
    );

  const changeMentionMode = (conversationId: string, modeValue: string) =>
    setMentions((current) =>
      current.map((item) =>
        item.reference.conversationId === conversationId
          ? { ...item, reference: conversationReference(conversationId, modeValue) }
          : item,
      ),
    );

  /** 每条的 token 数字来自**预览响应**（按 conversationId 对齐），前端不自算。 */
  const mentionTokens = (conversationId: string) =>
    mentionPreview?.items.find((item) => item.conversationId === conversationId)
      ?.tokenEstimate;
  const composerRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (variant === "side" && !loading && conversation) composerRef.current?.focus();
  }, [conversation, loading, variant]);

  const archived = conversation?.conversation.status === "archived";
  const defaultProvider = providers.find((item) => item.isDefault) ?? providers[0];
  const selectedProvider = providers.find(
    (item) => item.id === conversation?.conversation.providerProfileId,
  );
  const effectiveProvider = selectedProvider ?? defaultProvider;
  const selectedModelId =
    conversation?.conversation.modelOverride ?? effectiveProvider?.defaultModel ?? "";
  const selectedModel = effectiveProvider?.models.find(
    (model) => model.modelId === selectedModelId,
  );
  const providerUnavailable =
    !effectiveProvider ||
    !effectiveProvider.enabled ||
    !effectiveProvider.configured ||
    !selectedModel ||
    !selectedModel.enabled;
  const serviceReady = Boolean(health && !providerUnavailable);
  const serviceStatusLabel = !health
    ? "本地服务未连接"
    : !effectiveProvider
      ? "请选择模型"
      : !effectiveProvider.enabled
        ? "模型服务已停用"
        : !effectiveProvider.configured
          ? "需要配置模型服务"
          : !selectedModel || !selectedModel.enabled
            ? "当前模型不可用"
            : "模型服务可用";
  const composerDisabled =
    !conversation || archived || providerUnavailable || otherLaneRunning;
  const attachmentDisabled =
    !conversation || archived || isGenerating || pendingAction !== null;
  const selectableProviders = providers.filter(
    (item) =>
      item.enabled &&
      item.configured &&
      item.models.some((model) => model.enabled),
  );
  const explicitProviderId = conversation?.conversation.providerProfileId;
  const explicitSelectionUnavailable =
    Boolean(explicitProviderId) &&
    (!selectedProvider ||
      !selectedProvider.enabled ||
      !selectedProvider.configured ||
      !selectedModel ||
      !selectedModel.enabled);
  const modelSelectionValue = explicitSelectionUnavailable
    ? "__unavailable__"
    : explicitProviderId && selectedModelId && selectedModel
      ? JSON.stringify([explicitProviderId, selectedModelId])
      : "";

  const changeModel = (value: string) => {
    if (value === "__manage__") {
      onOpenProviders?.();
      return;
    }
    if (!value) {
      onModelChange(null, null);
      return;
    }
    const [providerProfileId, modelId] = JSON.parse(value) as [string, string];
    onModelChange(providerProfileId, modelId);
  };

  // 预览被后端明确拒绝（4xx）时**不发**：那是"这条引用一定不合法"的确定结论，
  // 让用户白跑一次提交（还会连带自动命名/技能解析等侧写入）没有意义。
  //
  // §6 裁定：**发送成功后清空 chip**——复审实测"不清空会让同一条引用被反复带进后续
  // 每一条消息"，那不是粘性选择而是静默重复注入。发送**失败**（返回 false / 抛错）
  // 时保留：别让用户重填。所以 `onSend` 必须是**能区分成败**的（返回 true 才算发出去了）。
  const handleSend = async () => {
    if (mentionSendBlocked) return;
    let delivered = false;
    try {
      delivered = (await onSend(references)) === true;
    } catch {
      // 调用方抛错 ⇒ 按"没发出去"处理：错误文案由发送路径自己显示（它已 catch 并展示），
      // 这里只保证**不吞掉用户的引用选择**。
      delivered = false;
    }
    if (delivered) {
      setMentions([]);
      setMentionPreview(null);
      setMentionPreviewError(null);
    }
  };

  return (
    <footer className="composer-region">
      <div className="composer">
        {variant !== "side" && conversation?.files.length ? (
          <div className="composer-files" aria-label="当前对话文件">
            {conversation.files.map((file) => (
              <span className="composer-file" key={file.id}>
                <FileIcon size={16} />
                <span title={file.originalName}>{file.originalName}</span>
                <button
                  aria-label={`移除 ${file.originalName}`}
                  disabled={attachmentDisabled}
                  onClick={() => onRemoveFile(file.id)}
                  type="button"
                >
                  <CloseIcon size={15} />
                </button>
              </span>
            ))}
          </div>
        ) : null}
        {mentions.length ? (
          <div className="composer-mentions" aria-label="已引用的会话上下文">
            {mentions.map((item) => (
              <span className="composer-mention" key={item.reference.conversationId}>
                <span aria-hidden="true" className="composer-mention-at">
                  @
                </span>
                <span className="composer-mention-title" title={item.title}>
                  {item.title}
                </span>
                <select
                  aria-label={`引用「${item.title}」的范围`}
                  className="composer-mention-mode"
                  onChange={(event) =>
                    changeMentionMode(
                      item.reference.conversationId,
                      event.target.value,
                    )
                  }
                  value={mentionModeValue(item.reference)}
                >
                  <option value="summary">摘要</option>
                  {RECENT_TURN_CHOICES.map((turns) => (
                    <option key={turns} value={`recent:${turns}`}>
                      最近 {turns} 轮
                    </option>
                  ))}
                </select>
                {typeof mentionTokens(item.reference.conversationId) === "number" ? (
                  <span className="composer-mention-tokens">
                    {mentionTokens(item.reference.conversationId)} tokens
                  </span>
                ) : null}
                <button
                  aria-label={`移除引用「${item.title}」`}
                  onClick={() => removeMention(item.reference.conversationId)}
                  type="button"
                >
                  <CloseIcon size={15} />
                </button>
              </span>
            ))}
            {/* 数字全部来自后端预览响应（R-C4 修订：前端不许自己造 token 数）。 */}
            {mentionPreview ? (
              <span className="composer-mention-total" role="status">
                引用 {mentions.length} 条 · 约 {mentionPreview.totalTokenEstimate} tokens
              </span>
            ) : null}
            {mentions.length > DEFAULT_MENTION_REFERENCES ? (
              <span className="composer-mention-warn" role="status">
                已选 {mentions.length} 条，超过默认上限 {DEFAULT_MENTION_REFERENCES} 条
              </span>
            ) : null}
            {mentionPreview?.warnings.length ? (
              <span className="composer-mention-warn" role="alert">
                {mentionPreview.warnings.join(" ")}
              </span>
            ) : null}
            {mentionPreviewError ? (
              <span className="composer-mention-error" role="alert">
                {mentionPreviewError.message}
              </span>
            ) : null}
            {steerable ? (
              <span className="composer-mention-warn" role="status">
                当前消息会作为纠偏指令发送，@ 引用本次不生效。
              </span>
            ) : null}
          </div>
        ) : null}
        <div className="composer-input-row" ref={inputRowRef}>
          {candidateMenuVisible && typeof document !== "undefined"
            ? createPortal(
                <div
                  className="composer-slash-menu-anchor"
                  ref={candidateMenuRef}
                  style={
                    {
                      top: candidateMenuPosition?.top ?? 0,
                      left: candidateMenuPosition?.left ?? 0,
                      width: candidateMenuPosition?.width ?? 420,
                      visibility: candidateMenuPosition ? "visible" : "hidden",
                    } as CSSProperties
                  }
                >
                  {mentionPanelVisible ? (
                    mentionBlockedReason ? (
                      <div className="composer-mention-menu is-empty" role="status">
                        <p>{mentionBlockedReason}</p>
                      </div>
                    ) : mentionCandidateOpen ? (
                      <>
                        <ul
                          aria-label="会话引用候选"
                          className={
                            mentionShowCount
                              ? "composer-mention-menu has-more"
                              : "composer-mention-menu"
                          }
                          role="listbox"
                        >
                          {mentionVisible.map((candidate, index) => (
                            <li key={candidate.id}>
                              <button
                                aria-selected={index === mentionActiveIndex}
                                className={
                                  index === mentionActiveIndex
                                    ? "is-active"
                                    : undefined
                                }
                                // 别把焦点从输入框抢走：否则点击瞬间菜单会关、方向键也断。
                                onMouseDown={(event) => event.preventDefault()}
                                onClick={() => acceptMention(candidate)}
                                role="option"
                                type="button"
                              >
                                <span className="composer-mention-name">
                                  {candidate.title}
                                </span>
                                <span className="composer-mention-desc">
                                  {mentionCandidateMeta(candidate)}
                                </span>
                              </button>
                            </li>
                          ))}
                        </ul>
                        {mentionShowCount ? (
                          <p className="composer-mention-more" role="status">
                            {mentionHiddenCount > 0
                              ? `共 ${mentionMatches.length} 个会话，已显示前 ${mentionVisible.length} 个，继续输入可缩小范围`
                              : `共 ${mentionMatches.length} 个会话，可滚动或用 ↑↓ 选择`}
                          </p>
                        ) : null}
                      </>
                    ) : (
                      <div className="composer-mention-menu is-empty" role="status">
                        <p>
                          {mentionQuery
                            ? `没有匹配“@${mentionQuery}”的会话`
                            : "还没有可以引用的其它会话"}
                        </p>
                        <p className="composer-slash-hint">
                          {mentionLoading
                            ? "正在加载会话列表…"
                            : "只能引用同一个工作区里、未归档的其它会话。"}
                        </p>
                      </div>
                    )
                  ) : null}
                  {slashEmpty ? (
                    <div className="composer-slash-menu is-empty" role="status">
                      <p>
                        {slashQuery
                          ? `没有匹配“/${slashQuery}”的技能`
                          : "还没有可调用的技能"}
                      </p>
                      <p className="composer-slash-hint">
                        放到 ~/.claude/skills、~/.agents/skills
                        或应用技能目录即可；也可在「技能」页签导入。
                      </p>
                    </div>
                  ) : null}
                  {slashOpen ? (
                    <ul
                      aria-label="技能候选"
                      className={
                        slashShowCount
                          ? "composer-slash-menu has-more"
                          : "composer-slash-menu"
                      }
                      role="listbox"
                    >
                      {slashVisible.map((candidate, index) => (
                        <li key={candidate.name}>
                          <button
                            aria-selected={index === slashActiveIndex}
                            className={
                              index === slashActiveIndex
                                ? "is-active"
                                : undefined
                            }
                            // 别把焦点从输入框抢走：否则点击瞬间菜单会关、方向键也断。
                            onMouseDown={(event) => event.preventDefault()}
                            onClick={() => acceptSlash(candidate.name)}
                            role="option"
                            type="button"
                          >
                            <span className="composer-slash-name">
                              /{candidate.name}
                            </span>
                            <span className="composer-slash-desc">
                              {candidate.description}
                            </span>
                            {candidate.pinned ? (
                              <span
                                className="composer-slash-pin"
                                title="已固定进模型的默认技能目录"
                              >
                                已固定
                              </span>
                            ) : null}
                            {candidate.source ? (
                              <span className="composer-slash-source">
                                {candidate.source}
                              </span>
                            ) : null}
                          </button>
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  {slashOpen && slashShowCount ? (
                    <p className="composer-slash-more" role="status">
                      {slashHiddenCount > 0
                        ? `共 ${slashMatches.length} 个技能，已显示前 ${slashVisible.length} 个，继续输入可缩小范围`
                        : `共 ${slashMatches.length} 个技能，可滚动或用 ↑↓ 选择`}
                    </p>
                  ) : null}
                </div>,
                document.body,
              )
            : null}
          <textarea
            aria-label="给 Endless 发送消息"
            ref={composerRef}
            disabled={composerDisabled || (isGenerating && !steerable)}
            onChange={(event) => {
              onDraftChange(event.target.value);
              syncSlash(event.target.value, event.target.selectionStart);
              syncMention(event.target.value, event.target.selectionStart);
            }}
            onKeyDown={(event) => {
              if (mentionCandidateOpen && mentionVisible.length > 0) {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setMentionIndex(
                    (current) => (current + 1) % mentionVisible.length,
                  );
                  return;
                }
                if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setMentionIndex(
                    (current) =>
                      (current - 1 + mentionVisible.length) %
                      mentionVisible.length,
                  );
                  return;
                }
                if (event.key === "Enter" || event.key === "Tab") {
                  event.preventDefault();
                  acceptMention(mentionVisible[mentionActiveIndex]);
                  return;
                }
              }
              if (mentionQuery !== null && event.key === "Escape") {
                event.preventDefault();
                setMentionQuery(null);
                return;
              }
              if (slashOpen && slashVisible.length > 0) {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setSlashIndex((current) => (current + 1) % slashVisible.length);
                  return;
                }
                if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setSlashIndex(
                    (current) =>
                      (current - 1 + slashVisible.length) % slashVisible.length,
                  );
                  return;
                }
                if (event.key === "Enter" || event.key === "Tab") {
                  event.preventDefault();
                  acceptSlash(slashVisible[slashActiveIndex].name);
                  return;
                }
                if (event.key === "Escape") {
                  event.preventDefault();
                  setSlashQuery(null);
                  return;
                }
              }
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void handleSend();
              }
            }}
            placeholder={
              archived
                ? "恢复对话后继续"
                : otherLaneRunning
                  ? `${runningLaneLabel}正在运行，请先查看或停止`
                  : steerable
                    ? "正在给 Endless 发送指令（可打断并纠偏当前运行）…"
                    : providerUnavailable
                      ? "请先配置模型服务"
                      : variant === "side"
                        ? sideMode === "branch_lane"
                          ? "在此分支中继续对话"
                          : "在临时对话中发送消息"
                        : "给 Endless 发送消息"
            }
            rows={1}
            value={draft}
          />
        </div>
        <div className="composer-toolbar">
          <div className="composer-tools">
            {contextBudget ? (
              <ContextBudgetMeter budget={contextBudget} />
            ) : null}
            {variant === "side" ? null : (
              <>
                <input
                  ref={fileInputRef}
                  accept=".txt,.md,.markdown,.json,.csv,.tsv,.py,.js,.jsx,.ts,.tsx,.html,.css,.yaml,.yml,.toml"
                  className="file-input"
                  disabled={attachmentDisabled}
                  onChange={(event) => {
                    const file = event.target.files?.[0];
                    if (file) onUploadFile(file);
                    event.target.value = "";
                  }}
                  type="file"
                />
                <button
                  aria-label="添加文本文件"
                  className="composer-tool-button attach-button"
                  disabled={attachmentDisabled}
                  onClick={() => fileInputRef.current?.click()}
                  title="添加文本文件"
                  type="button"
                >
                  <AddIcon size={20} />
                </button>
              </>
            )}
            <span
              aria-hidden="true"
              className={
                serviceReady
                  ? "status-light"
                  : "status-light is-warning"
              }
            />
            <span
              aria-atomic="true"
              aria-live="polite"
              className={`composer-service-status${
                serviceReady ? " is-ready" : ""
              }`}
              role="status"
            >
              {serviceStatusLabel}
            </span>
            <select
              aria-label="当前对话模型"
              className="model-select"
              disabled={!conversation || archived || pendingAction !== null}
              onChange={(event) => changeModel(event.target.value)}
              title="选择当前对话使用的模型"
              value={modelSelectionValue}
            >
              <option
                disabled={
                  !defaultProvider ||
                  !defaultProvider.enabled ||
                  !defaultProvider.configured ||
                  !defaultProvider.models.some(
                    (model) =>
                      model.modelId === defaultProvider.defaultModel && model.enabled,
                  )
                }
                value=""
              >
                {defaultProvider?.defaultModel
                  ? `默认 · ${defaultProvider.name} · ${defaultProvider.defaultModel}`
                  : "默认模型不可用"}
              </option>
              {explicitSelectionUnavailable ? (
                <option disabled value="__unavailable__">
                  当前选择不可用，请重新选择
                </option>
              ) : null}
              {selectableProviders.map((provider) => (
                <optgroup key={provider.id} label={provider.name}>
                  {provider.models
                    .filter((model) => model.enabled)
                    .map((model) => (
                      <option
                        key={`${provider.id}:${model.modelId}`}
                        value={JSON.stringify([provider.id, model.modelId])}
                      >
                        {model.displayName}
                      </option>
                    ))}
                </optgroup>
              ))}
              <option value="__manage__">管理模型服务…</option>
            </select>
          </div>
          {isGenerating ? (
            <>
              {steerable ? (
                <button
                  aria-label="发送指令（打断并纠偏）"
                  className="send-button"
                  disabled={composerDisabled || !draft.trim()}
                  onClick={handleSend}
                  type="button"
                >
                  <SendIcon size={20} />
                </button>
              ) : null}
              <button
                aria-label="停止生成"
                className="send-button stop-button"
                disabled={pendingAction === "cancel"}
                onClick={onCancel}
                type="button"
              >
                <span aria-hidden="true" />
              </button>
            </>
          ) : (
            <button
              aria-label="发送消息"
              className="send-button"
              disabled={
                composerDisabled ||
                !draft.trim() ||
                pendingAction !== null ||
                mentionSendBlocked
              }
              onClick={handleSend}
              type="button"
            >
              <SendIcon size={20} />
            </button>
          )}
        </div>
      </div>
      {/*
        C3b（I-1）：这是本批**唯一**的用户可见提示——`placeholder` 里没有 `@`、工具栏
        没有入口、浮层只有敲了 `@` 才出现，所以这行文字删了就没人能发现这个功能。

        契约：**侧栏与主区的 `.composer` 底边对齐**（`e2e/desktop/branch-temporary.spec.ts:91`
        断言 `mainComposerBottom ≈ sideComposerBottom`）。侧栏那份提示是
        `visibility: hidden`（`styles/chat.css` 的 `.chat-surface.is-side .composer-hint`）
        ——**隐藏仍占布局盒**，所以侧栏文案比自己多折一行就会把 footer 顶高、把 `.composer`
        底边上移一个行高（实测 17px），e2e 会红。
        ⇒ 侧栏文案必须与基线逐字一致：只在**主区**（`variant !== "side"`）拼上 `@` 提示。
        另：`jsdom` 不加载 `layout.css/chat.css`、不算布局 ⇒ **这条契约只由 e2e 守护**，
        单测层看不见（所以本批的 composer 改动曾一路全绿踩坏它）。
      */}
      <p className="composer-hint">
        {`Enter 发送 · Shift + Enter 换行${
          variant !== "side" ? " · @ 引用会话上下文" : ""
        } · 可附加 UTF-8 文本（≤ 1 MB）`}
      </p>
    </footer>
  );
}