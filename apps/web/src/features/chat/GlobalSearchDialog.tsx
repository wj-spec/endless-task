import { useEffect, useMemo, useRef, useState } from "react";
import { chatApi } from "./api";
import type {
  GlobalSearchGroup,
  GlobalSearchHit,
  GlobalSearchScope,
} from "./apiTypes";

const GROUP_LABELS: Record<GlobalSearchGroup["scope"], string> = {
  conversation: "会话",
  source: "知识来源",
  memory: "记忆",
  artifact: "成果",
};

const SEARCH_DEBOUNCE_MS = 280;
const MIN_QUERY_LENGTH = 1;

type GlobalSearchDialogProps = {
  onClose: () => void;
  onOpenConversation: (conversationId: string) => void;
  onOpenAssistantTab: (tab: "knowledge" | "memory") => void;
  onOpenWorkspace: () => void;
};

export function GlobalSearchDialog({
  onClose,
  onOpenConversation,
  onOpenAssistantTab,
  onOpenWorkspace,
}: GlobalSearchDialogProps) {
  const [query, setQuery] = useState("");
  const [groups, setGroups] = useState<GlobalSearchGroup[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestSeq = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const trimmed = query.trim();

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  useEffect(() => {
    if (!trimmed) {
      setGroups(null);
      setLoading(false);
      setError(null);
      return;
    }
    const seq = requestSeq.current + 1;
    requestSeq.current = seq;
    setLoading(true);
    const timer = globalThis.setTimeout(() => {
      void chatApi
        .searchGlobal(trimmed)
        .then((nextGroups) => {
          if (requestSeq.current !== seq) return;
          setGroups(nextGroups);
          setError(null);
        })
        .catch((searchError: unknown) => {
          if (requestSeq.current !== seq) return;
          setGroups(null);
          setError(
            searchError instanceof Error ? searchError.message : "搜索失败，请重试。",
          );
        })
        .finally(() => {
          if (requestSeq.current === seq) setLoading(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => globalThis.clearTimeout(timer);
  }, [trimmed]);

  const firstConversationHit = useMemo(
    () =>
      groups?.find((group) => group.scope === "conversation")?.hits[0] ?? null,
    [groups],
  );

  const handleHitAction = (scope: GlobalSearchScope, hit: GlobalSearchHit) => {
    if (scope === "conversation") {
      if (hit.conversationId) {
        onClose();
        onOpenConversation(hit.conversationId);
      }
      return;
    }
    onClose();
    if (scope === "artifact") {
      onOpenWorkspace();
      return;
    }
    onOpenAssistantTab(scope === "memory" ? "memory" : "knowledge");
  };

  const openFirstConversation = () => {
    const hit = firstConversationHit;
    if (hit?.conversationId) {
      onClose();
      onOpenConversation(hit.conversationId);
    }
  };

  return (
    <div className="global-search-backdrop" onMouseDown={onClose}>
      <div
        aria-label="全局搜索"
        aria-modal="true"
        className="global-search-dialog"
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <div className="global-search-input-row">
          <input
            aria-label="搜索所有会话、知识、记忆与成果"
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") openFirstConversation();
              if (event.key === "Escape") onClose();
            }}
            placeholder="搜索所有会话、知识、记忆与成果…"
            ref={inputRef}
            type="search"
            value={query}
          />
          <button aria-label="关闭全局搜索" className="global-search-close" onClick={onClose} type="button">
            ✕
          </button>
        </div>
        <div className="global-search-hint" role="note">
          {trimmed ? "回车打开第一个会话结果 · Esc 关闭" : "输入至少 1 个字符开始搜索"}
        </div>

        {error ? (
          <p className="global-search-error" role="alert">
            {error}
          </p>
        ) : null}
        {loading && !groups ? (
          <p className="global-search-status">正在搜索…</p>
        ) : null}

        {!loading && groups && groups.length === 0 ? (
          <p className="global-search-empty">未找到匹配内容。</p>
        ) : null}

        {groups
          ? groups.map((group) => (
              <section aria-label={GROUP_LABELS[group.scope]} className="global-search-group" key={group.scope}>
                <h2>{GROUP_LABELS[group.scope]}</h2>
                <ul>
                  {group.hits.map((hit, index) => (
                    <li key={`${group.scope}-${hit.refId}-${index}`}>
                      <button
                        onClick={() => handleHitAction(group.scope, hit)}
                        type="button"
                      >
                        <span className="global-search-hit-title">{hit.title || "（无标题）"}</span>
                        {hit.snippet ? (
                          <span className="global-search-hit-snippet">{hit.snippet}</span>
                        ) : null}
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            ))
          : null}
      </div>
    </div>
  );
}
