import { useEffect, useMemo, useRef, useState } from "react";
import type {
  ConversationSnapshot,
  HealthSnapshot,
  ProviderProfile,
  RuntimeV2Snapshot,
  SkillInvocationCandidate,
} from "./apiTypes";
import { ContextBudgetMeter } from "./ContextBudgetMeter";
import { AddIcon, CloseIcon, FileIcon, SendIcon } from "../ui/Icons";

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
  onSend: () => void;
  onUploadFile: (file: File) => void;
  /** A2：上下文预算（环形指示器，输入框左下角）。 */
  contextBudget?: RuntimeV2Snapshot["contextBudget"];
  /** S1：`/技能名` 候选。 */
  skillCandidates?: SkillInvocationCandidate[];
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
}: ChatComposerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [slashQuery, setSlashQuery] = useState<string | null>(null);
  const [slashIndex, setSlashIndex] = useState(0);

  const slashMatches = useMemo(() => {
    if (slashQuery === null) return [];
    const query = slashQuery.toLowerCase();
    return skillCandidates
      .filter((candidate) => candidate.name.toLowerCase().startsWith(query))
      .slice(0, 8);
  }, [skillCandidates, slashQuery]);

  const slashOpen = slashQuery !== null && slashMatches.length > 0;

  /** 只在"行首或空白后的 /token"上触发候选（避免路径误触发）。 */
  const syncSlash = (value: string, caret: number | null) => {
    const upto = caret === null ? value.length : caret;
    const match = /(^|\s)\/([a-z0-9-]*)$/.exec(value.slice(0, upto));
    setSlashQuery(match ? match[2] : null);
    setSlashIndex(0);
  };

  const acceptSlash = (name: string) => {
    const textarea = composerRef.current;
    const caret = textarea?.selectionStart ?? draft.length;
    const before = draft.slice(0, caret);
    const match = /(^|\s)\/([a-z0-9-]*)$/.exec(before);
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
        <div className="composer-input-row">
          {slashOpen ? (
            <ul
              aria-label="技能候选"
              className="composer-slash-menu"
              role="listbox"
            >
              {slashMatches.map((candidate, index) => (
                <li key={candidate.name}>
                  <button
                    aria-selected={index === slashIndex}
                    className={index === slashIndex ? "is-active" : undefined}
                    onClick={() => acceptSlash(candidate.name)}
                    role="option"
                    type="button"
                  >
                    <span className="composer-slash-name">/{candidate.name}</span>
                    <span className="composer-slash-desc">{candidate.description}</span>
                  </button>
                </li>
              ))}
            </ul>
          ) : null}
          <textarea
            aria-label="给 Endless 发送消息"
            ref={composerRef}
            disabled={composerDisabled || (isGenerating && !steerable)}
            onChange={(event) => {
              onDraftChange(event.target.value);
              syncSlash(event.target.value, event.target.selectionStart);
            }}
            onKeyDown={(event) => {
              if (slashOpen) {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setSlashIndex((current) => (current + 1) % slashMatches.length);
                  return;
                }
                if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setSlashIndex(
                    (current) =>
                      (current - 1 + slashMatches.length) % slashMatches.length,
                  );
                  return;
                }
                if (event.key === "Enter" || event.key === "Tab") {
                  event.preventDefault();
                  acceptSlash(slashMatches[slashIndex].name);
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
                onSend();
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
                  onClick={onSend}
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
              disabled={composerDisabled || !draft.trim() || pendingAction !== null}
              onClick={onSend}
              type="button"
            >
              <SendIcon size={20} />
            </button>
          )}
        </div>
      </div>
      <p className="composer-hint">
        Enter 发送 · Shift + Enter 换行 · 可附加 UTF-8 文本（≤ 1 MB）
      </p>
    </footer>
  );
}