import type { KnowledgeCitation } from "./apiTypes";
import { CloseIcon } from "../ui/Icons";

const SCOPE_LABELS: Record<KnowledgeCitation["scope"], string> = {
  source: "知识源",
  memory: "记忆",
  artifact: "成果",
  conversation: "历史对话",
};

const JUMP_LABELS: Record<KnowledgeCitation["scope"], string> = {
  source: "去知识标签页查看",
  memory: "去记忆标签页查看",
  artifact: "去成果面板查看",
  conversation: "打开来源会话",
};

type SourceQuality = NonNullable<KnowledgeCitation["sourceQuality"]>;

const sourceQualityText = (q: SourceQuality): string => {
  const parts: string[] = [];
  if (q.recencyDays === 0) parts.push("最新");
  else if (typeof q.recencyDays === "number" && q.recencyDays <= 7) parts.push(`${q.recencyDays} 天内`);
  else if (typeof q.recencyDays === "number" && q.recencyDays <= 30) parts.push(`${q.recencyDays} 天前`);
  else if (typeof q.recencyDays === "number") parts.push("较早");
  if (q.authority) parts.push(`${q.authority}权威`);
  if (typeof q.relevance === "number") parts.push(`相关 ${q.relevance}`);
  return parts.join(" · ") || "来源质量未知";
};

export function CitationCard({
  citation,
  jumpDisabled,
  onClose,
  onJump,
}: {
  citation: KnowledgeCitation | null;
  jumpDisabled?: boolean;
  onClose: () => void;
  onJump: (citation: KnowledgeCitation) => void;
}) {
  return (
    <div className="citation-card" role="note">
      <div className="citation-card-head">
        {citation ? (
          <span className={`citation-card-scope is-${citation.scope}`}>
            [{citation.label}] {SCOPE_LABELS[citation.scope]}
          </span>
        ) : (
          <span className="citation-card-scope">引用</span>
        )}
        <button
          aria-label="关闭引用卡片"
          className="citation-card-close"
          onClick={onClose}
          type="button"
        >
          <CloseIcon size={16} />
        </button>
      </div>

      {citation ? (
        <>
          <div className="citation-card-title">
            {citation.title}
            {typeof citation.chunkSeq === "number"
              ? ` · 第 ${citation.chunkSeq + 1} 段`
              : ""}
          </div>
          {citation.snippet ? (
            <p className="citation-card-snippet">{citation.snippet}</p>
          ) : null}
          {citation.sourceQuality ? (
            <p className="citation-card-quality">
              {sourceQualityText(citation.sourceQuality)}
            </p>
          ) : null}
          <button
            className="citation-card-jump"
            disabled={jumpDisabled}
            onClick={() => onJump(citation)}
            type="button"
          >
            {JUMP_LABELS[citation.scope]}
          </button>
        </>
      ) : (
        <p className="citation-card-unresolved">
          这条引用没有匹配到本轮真正注入的来源，可能是模型未依据真实知识生成的占位引用。
        </p>
      )}
    </div>
  );
}
