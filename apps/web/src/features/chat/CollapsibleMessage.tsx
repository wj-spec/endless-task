import { useState } from "react";
import { MessageContent } from "./MessageContent";

const COLLAPSE_THRESHOLD = 1200;
const PREVIEW_LENGTH = 600;

type CollapsibleMessageProps = {
  content: string;
  forceExpand?: boolean;
  streaming: boolean;
  onCitationClick?: (label: string) => void;
  resolvableCitationLabels?: Set<string>;
};

/**
 * 生成折叠预览：只在“安全边界”处截断，避免把粗体/代码/引用/列表切在 token 中间。
 * 安全边界 = 空行、刚闭合的围栏行；无自然边界时退化：巨型单行按空白切、连续行按整行切，
 * 并保证预览内围栏成对（必要时补闭合）。
 */
export function collapsePreview(content: string, previewChars = PREVIEW_LENGTH): string {
  if (content.length <= previewChars) return content;

  const lines = content.split("\n");
  let consumed = 0;
  let boundaryAt = -1;
  let stopAt = lines.length;
  let fenceOpen = false;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const isFence = /^\s*```/.test(line);
    consumed += line.length + 1;
    if (consumed > previewChars) {
      stopAt = index;
      break;
    }
    if (isFence) fenceOpen = !fenceOpen;
    // 空行、或刚由“开→关”翻转完成的围栏闭合行 = 安全断点
    if (line.trim() === "" || (isFence && !fenceOpen)) boundaryAt = index;
  }

  let included: string[];
  if (boundaryAt >= 0) {
    included = lines.slice(0, boundaryAt + 1);
  } else if (stopAt === 0) {
    // 巨型单行：按空白切，保留词完整性（不在单词中间截断）
    const line = lines[0];
    const cutIndex = line.lastIndexOf(" ", previewChars);
    const at = cutIndex > previewChars / 2 ? cutIndex : previewChars;
    return `${line.slice(0, at).trimEnd()}…`;
  } else {
    included = lines.slice(0, stopAt);
  }

  const preview = included.join("\n").replace(/\n+$/, "");
  const openInPreview = included.reduce(
    (open, line) => (/^\s*```/.test(line) ? !open : open),
    false,
  );
  return openInPreview ? `${preview}…\n\`\`\`` : `${preview}…`;
}

export function CollapsibleMessage({
  content,
  forceExpand = false,
  streaming,
  onCitationClick,
  resolvableCitationLabels,
}: CollapsibleMessageProps) {
  const [expanded, setExpanded] = useState(false);
  const collapsible = !streaming && content.length > COLLAPSE_THRESHOLD;
  const effectiveExpanded = expanded || forceExpand;
  const collapsed = collapsible && !effectiveExpanded;

  const shown = collapsed ? collapsePreview(content) : content;

  return (
    <div
      className={
        streaming ? "collapsible-message is-streaming" : "collapsible-message"
      }
    >
      <MessageContent
        content={shown}
        onCitationClick={onCitationClick}
        resolvableCitationLabels={resolvableCitationLabels}
      />
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
