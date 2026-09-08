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
