import { useCallback, useEffect, useState } from "react";
import {
  ApiClientError,
  chatApi,
  downloadArtifactExport,
  type ExportFormat,
} from "../chat/api";
import type { ArtifactDetailSnapshot, ArtifactVersionRecord } from "../chat/apiTypes";
import { MessageContent } from "../chat/MessageContent";
import { formatRelativeTime } from "./time";

type ArtifactDetailProps = {
  artifactId: string;
  conversationId: string;
  latestTurnId: string | null;
  onBack: () => void;
  onChanged: () => void;
};

const kindLabel = { markdown: "文档", text: "纯文本" } as const;

const operationLabel = {
  create: "创建",
  update: "更新",
  chat_continue: "聊天继续",
  rollback: "回滚",
} as const;

const EXPORT_FORMATS: { format: ExportFormat; label: string }[] = [
  { format: "markdown", label: "Markdown" },
  { format: "html", label: "HTML" },
  { format: "pdf", label: "PDF" },
];

export function ArtifactDetail({
  artifactId,
  conversationId,
  latestTurnId,
  onBack,
  onChanged,
}: ArtifactDetailProps) {
  const [detail, setDetail] = useState<ArtifactDetailSnapshot | null>(null);
  const [versions, setVersions] = useState<ArtifactVersionRecord[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<ExportFormat | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [confirmingOrdinal, setConfirmingOrdinal] = useState<number | null>(null);
  const [rollingBack, setRollingBack] = useState(false);
  const [rollbackError, setRollbackError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [snapshot, versionList] = await Promise.all([
        chatApi.getArtifact(artifactId),
        chatApi.listArtifactVersions(artifactId),
      ]);
      setDetail(snapshot);
      setVersions(versionList);
      setLoadError(null);
    } catch {
      setLoadError("无法加载 Artifact 详情，请重试。");
    }
  }, [artifactId]);

  useEffect(() => {
    setDetail(null);
    setVersions([]);
    setConfirmingOrdinal(null);
    setExportError(null);
    setRollbackError(null);
    void load();
  }, [load]);

  const handleExport = async (format: ExportFormat) => {
    setExporting(format);
    setExportError(null);
    try {
      await downloadArtifactExport(artifactId, format);
    } catch (error) {
      setExportError(
        error instanceof ApiClientError && error.status === 503
          ? "PDF 渲染依赖不可用，请改用 HTML 导出。"
          : "导出失败，请重试。",
      );
    } finally {
      setExporting(null);
    }
  };

  const handleRollback = async (targetOrdinal: number) => {
    if (!latestTurnId) return;
    setRollingBack(true);
    setRollbackError(null);
    try {
      await chatApi.rollbackArtifact(artifactId, {
        targetOrdinal,
        sourceConversationId: conversationId,
        sourceTurnId: latestTurnId,
      });
      setConfirmingOrdinal(null);
      await load();
      onChanged();
    } catch (error) {
      setRollbackError(
        error instanceof ApiClientError ? error.message : "回滚失败，请重试。",
      );
    } finally {
      setRollingBack(false);
    }
  };

  const currentOrdinal = detail?.artifact.currentVersionOrdinal ?? null;
  const timeline = [...versions].sort((left, right) => right.ordinal - left.ordinal);

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
            <div className="artifact-toolbar" aria-label="导出">
              <span className="artifact-toolbar-label">导出</span>
              {EXPORT_FORMATS.map(({ format, label }) => (
                <button
                  disabled={exporting !== null}
                  key={format}
                  onClick={() => void handleExport(format)}
                  type="button"
                >
                  {exporting === format ? "导出中…" : label}
                </button>
              ))}
            </div>
            {exportError ? (
              <div className="proposal-error" role="alert">
                {exportError}
              </div>
            ) : null}
          </>
        ) : null}
      </div>
      <div className="workspace-detail-body">
        {loadError ? <div className="workspace-detail-state">{loadError}</div> : null}
        {!detail && !loadError ? (
          <div className="workspace-detail-state">正在加载…</div>
        ) : null}
        {detail ? <MessageContent content={detail.currentVersion.content} /> : null}

        {detail && versions.length > 0 ? (
          <details className="version-timeline" open={versions.length > 1}>
            <summary>版本时间线（{versions.length}）</summary>
            {latestTurnId === null ? (
              <p className="version-hint">回滚需要在会话中至少有一条消息。</p>
            ) : null}
            {rollbackError ? (
              <div className="proposal-error" role="alert">
                {rollbackError}
              </div>
            ) : null}
            <ol>
              {timeline.map((version) => {
                const isCurrent = version.ordinal === currentOrdinal;
                const isConfirming = confirmingOrdinal === version.ordinal;
                return (
                  <li
                    className={isCurrent ? "is-current" : ""}
                    key={version.id}
                  >
                    <div className="version-line">
                      <span className="version-ordinal">v{version.ordinal}</span>
                      <span className="version-operation">
                        {operationLabel[version.operation]}
                      </span>
                      <span className="version-time">
                        {formatRelativeTime(version.createdAt)}
                      </span>
                      {isCurrent ? (
                        <span className="version-current-mark">当前</span>
                      ) : (
                        <button
                          disabled={rollingBack || latestTurnId === null}
                          onClick={() =>
                            setConfirmingOrdinal(
                              isConfirming ? null : version.ordinal,
                            )
                          }
                          type="button"
                        >
                          {isConfirming ? "取消" : "回到此版本"}
                        </button>
                      )}
                    </div>
                    {isConfirming ? (
                      <div className="version-confirm">
                        <p>
                          将回到 v{version.ordinal}（
                          {operationLabel[version.operation]} ·{" "}
                          {formatRelativeTime(version.createdAt)}
                          ）。会追加为新版本，之后仍可再次回滚。
                        </p>
                        <div className="proposal-actions">
                          <button
                            disabled={rollingBack}
                            onClick={() => void handleRollback(version.ordinal)}
                            type="button"
                          >
                            {rollingBack ? "回滚中…" : "确认回滚"}
                          </button>
                          <button
                            disabled={rollingBack}
                            onClick={() => setConfirmingOrdinal(null)}
                            type="button"
                          >
                            取消
                          </button>
                        </div>
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ol>
          </details>
        ) : null}
      </div>
    </div>
  );
}
