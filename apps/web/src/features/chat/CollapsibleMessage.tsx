import { useState } from "react";
import { MessageContent } from "./MessageContent";

const COLLAPSE_THRESHOLD = 1200;
const PREVIEW_LENGTH = 600;

type CollapsibleMessageProps = {
  content: string;
  forceExpand?: boolean;
  streaming: boolean;
  onCitationClick?: (label: string) => void;
};

export function CollapsibleMessage({
  content,
  forceExpand = false,
  streaming,
  onCitationClick,
}: CollapsibleMessageProps) {
  const [expanded, setExpanded] = useState(false);
  const collapsible = !streaming && content.length > COLLAPSE_THRESHOLD;
  const effectiveExpanded = expanded || forceExpand;
  const collapsed = collapsible && !effectiveExpanded;

  let shown = content;
  if (collapsed) {
    shown = `${content.slice(0, PREVIEW_LENGTH)}…`;
    const fenceCount = (shown.match(/```/g) ?? []).length;
    if (fenceCount % 2 === 1) shown += "\n```";
  }

  return (
    <div
      className={
        streaming ? "collapsible-message is-streaming" : "collapsible-message"
      }
    >
      <MessageContent content={shown} onCitationClick={onCitationClick} />
      {collapsible ? (
        <button
          className="expand-toggle"
          onClick={() => setExpanded((current) => !current)}
          type="button"
        >
          {effectiveExpanded ? "收起全文" : "展开全文"}
        </button>
      ) : null}
    </div>
  );
}
