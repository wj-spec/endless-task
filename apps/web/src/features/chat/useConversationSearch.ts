import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import type {
  ConversationSnapshot,
  LiveTurn,
  ResponseVariantSnapshot,
} from "./apiTypes";

const EXCLUDE_SELECTOR =
  ".expand-toggle, .activity-line, .approval-prompt, .turn-notice, .response-actions, .thinking-line";

function findActiveVariant(
  variants: ResponseVariantSnapshot[],
  activeId: string | null,
) {
  return variants.find((item) => item.variant.id === activeId) ?? variants.at(-1);
}

function clearMarks(root: HTMLElement) {
  root.querySelectorAll("mark.search-hit").forEach((mark) => {
    const parent = mark.parentNode;
    if (!parent) return;
    parent.replaceChild(document.createTextNode(mark.textContent ?? ""), mark);
    parent.normalize();
  });
}

function highlightMatches(root: HTMLElement, query: string): number {
  const lower = query.toLowerCase();
  let count = 0;
  const boxes = root.querySelectorAll(".user-copy, .assistant-content");
  boxes.forEach((box) => {
    const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
    const textNodes: Text[] = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      textNodes.push(node as Text);
    }
    for (const textNode of textNodes) {
      const owner = textNode.parentElement;
      if (owner && owner.closest(EXCLUDE_SELECTOR)) continue;
      const text = textNode.nodeValue ?? "";
      const lowerText = text.toLowerCase();
      let index = lowerText.indexOf(lower);
      if (index === -1) continue;
      const fragment = document.createDocumentFragment();
      let cursor = 0;
      while (index !== -1) {
        fragment.appendChild(document.createTextNode(text.slice(cursor, index)));
        const mark = document.createElement("mark");
        mark.className = "search-hit";
        mark.textContent = text.slice(index, index + query.length);
        fragment.appendChild(mark);
        count += 1;
        cursor = index + query.length;
        index = lowerText.indexOf(lower, cursor);
      }
      fragment.appendChild(document.createTextNode(text.slice(cursor)));
      textNode.parentNode?.replaceChild(fragment, textNode);
    }
  });
  return count;
}

export function useConversationSearch(
  conversation: ConversationSnapshot | null,
  liveTurns: Record<string, LiveTurn>,
  containerRef: RefObject<HTMLDivElement | null>,
) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [current, setCurrent] = useState(0);
  const [hitCount, setHitCount] = useState(0);

  const conversationId = conversation?.conversation.id;

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query), 150);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    setCurrent(debouncedQuery ? 1 : 0);
  }, [debouncedQuery]);

  const close = useCallback(() => {
    setOpen(false);
    setQuery("");
    setDebouncedQuery("");
    setCurrent(0);
    setHitCount(0);
  }, []);

  useEffect(() => {
    close();
  }, [conversationId, close]);

  const openSearch = useCallback(() => {
    if (!conversationId) return;
    setOpen(true);
  }, [conversationId]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "f") {
        if (!conversationId) return;
        event.preventDefault();
        setOpen(true);
        return;
      }
      if (event.key === "Escape" && open) {
        close();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [conversationId, open, close]);

  const forceExpandTurnIds = useMemo(() => {
    const ids = new Set<string>();
    if (!open || !debouncedQuery || !conversation) return ids;
    const lower = debouncedQuery.toLowerCase();
    for (const snapshot of conversation.turns) {
      const variant = findActiveVariant(
        snapshot.responseVariants,
        snapshot.turn.activeResponseVariantId,
      );
      const live = liveTurns[snapshot.turn.id];
      const assistant =
        live && variant && live.responseVariantId === variant.variant.id
          ? live.content
          : (variant?.assistantMessage.content ?? "");
      if (
        snapshot.userMessage.content.toLowerCase().includes(lower) ||
        assistant.toLowerCase().includes(lower)
      ) {
        ids.add(snapshot.turn.id);
      }
    }
    return ids;
  }, [open, debouncedQuery, conversation, liveTurns]);

  const scrollKeyRef = useRef("");

  useEffect(() => {
    const root = containerRef.current;
    if (!root) return;
    clearMarks(root);
    if (!open || !debouncedQuery) {
      setHitCount(0);
      scrollKeyRef.current = "";
      return;
    }
    const total = highlightMatches(root, debouncedQuery);
    setHitCount(total);
    const marks = root.querySelectorAll("mark.search-hit");
    marks.forEach((mark) => mark.classList.remove("is-current"));
    if (total > 0 && current >= 1) {
      const target = marks[(current - 1) % total] as HTMLElement;
      target.classList.add("is-current");
      const key = `${debouncedQuery}::${current}`;
      if (scrollKeyRef.current !== key) {
        scrollKeyRef.current = key;
        target.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    } else {
      scrollKeyRef.current = "";
    }
  }, [open, debouncedQuery, conversation, liveTurns, forceExpandTurnIds, current, containerRef]);

  const next = useCallback(() => {
    if (!hitCount) return;
    setCurrent((value) => (value % hitCount) + 1);
  }, [hitCount]);

  const prev = useCallback(() => {
    if (!hitCount) return;
    setCurrent((value) => ((value - 2 + hitCount) % hitCount) + 1);
  }, [hitCount]);

  return {
    open,
    query,
    current,
    hitCount,
    forceExpandTurnIds,
    openSearch,
    close,
    next,
    prev,
    setQuery,
  };
}
