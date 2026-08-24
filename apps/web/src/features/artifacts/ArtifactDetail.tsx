import { useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import type { ArtifactDetailSnapshot } from "../chat/apiTypes";
import { MessageContent } from "../chat/MessageContent";
import { formatRelativeTime } from "./time";

type ArtifactDetailProps = {
  artifactId: string;
  onBack: () => void;
};

const kindLabel = { markdown: "文档", text: "纯文本" } as const;

export function ArtifactDetail({ artifactId, onBack }: ArtifactDetailProps) {
  const [detail, setDetail] = useState<ArtifactDetailSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setError(null);
    chatApi
      .getArtifact(artifactId)
      .then((snapshot) => {
        if (!cancelled) setDetail(snapshot);
      })
      .catch(() => {
        if (!cancelled) setError("无法加载 Artifact 详情，请重试。");
      });
    return () => {
      cancelled = true;
    };
  }, [artifactId]);

  return (
    <div className="workspace-detail">
      <div className="workspace-detail-header">
        <button className="workspace-back" onClick={onBack} type="button">
          ← 返回列表
        </button>
        {detail ? (
          <>
            <h3 className="workspace-detail-title">{detail.artifact.title}</h3>
            <div className="workspace-detail-meta">
              <span className="proposal-kind">{kindLabel[detail.artifact.kind]}</span>
              v{detail.currentVersion.ordinal} · 更新于{" "}
              {formatRelativeTime(detail.artifact.updatedAt)}
            </div>
          </>
        ) : null}
      </div>
      <div className="workspace-detail-body">
        {error ? <div className="workspace-detail-state">{error}</div> : null}
        {!detail && !error ? (
          <div className="workspace-detail-state">正在加载…</div>
        ) : null}
        {detail ? <MessageContent content={detail.currentVersion.content} /> : null}
      </div>
    </div>
  );
}
