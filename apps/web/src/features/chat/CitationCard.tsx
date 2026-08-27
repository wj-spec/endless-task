import type { KnowledgeCitation } from "./apiTypes";

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
      {citation ? (
        <>
          <div className="citation-card-head">
            <span className="citation-card-scope">
              [{citation.label}] {SCOPE_LABELS[citation.scope]}
            </span>
            <button
              aria-label="关闭引用卡片"
              className="citation-card-close"
              onClick={onClose}
              type="button"
            >
              ×
            </button>
          </div>
          <div className="citation-card-title">
            {citation.title}
            {typeof citation.chunkSeq === "number"
              ? ` · 第 ${citation.chunkSeq + 1} 段`
              : ""}
          </div>
          {citation.snippet ? (
            <p className="citation-card-snippet">{citation.snippet}</p>
          ) : null}
          <button
            disabled={jumpDisabled}
            onClick={() => onJump(citation)}
            type="button"
          >
            {JUMP_LABELS[citation.scope]}
          </button>
        </>
      ) : (
        <>
          <div className="citation-card-head">
            <span className="citation-card-scope">引用来源</span>
            <button
              aria-label="关闭引用卡片"
              className="citation-card-close"
              onClick={onClose}
              type="button"
            >
              ×
            </button>
          </div>
          <p className="citation-card-snippet">未找到这条引用的来源记录。</p>
        </>
      )}
    </div>
  );
}
