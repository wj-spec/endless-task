import { useCallback, useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import { RefreshIcon } from "../ui/Icons";
import type {
  TrajectoryBundleSummary,
  TrajectoryMeta,
} from "../chat/apiTypes";

const readFileSize = (bytes: number): string =>
  bytes > 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`;

/** 开发/诊断向：失败自动导出的 trajectory bundle 只读查看（P2-1b）。 */
export function TrajectoryViewer() {
  const [items, setItems] = useState<TrajectoryBundleSummary[]>([]);
  const [metaByRun, setMetaByRun] = useState<Record<string, TrajectoryMeta>>({});
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [opened, setOpened] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await chatApi.listTrajectoryBundles());
      setError(null);
    } catch {
      setError("无法加载轨迹包列表，请重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openBundle = async (runId: string) => {
    if (opened === runId) {
      setOpened(null);
      return;
    }
    setOpened(runId);
    if (metaByRun[runId]) return;
    try {
      const meta = await chatApi.getTrajectoryMeta(runId);
      setMetaByRun((current) => ({ ...current, [runId]: meta }));
    } catch {
      setError("无法读取轨迹包元信息，请重试。");
    }
  };

  return (
    <section aria-label="轨迹包" className="runtime-v2-trajectory">
      <div className="runtime-v2-trajectory-head">
        <strong>轨迹包（诊断）</strong>
        <button
          aria-label="刷新列表"
          onClick={() => void load()}
          type="button"
        >
          <RefreshIcon size={13} />
          刷新
        </button>
      </div>
      {error ? <p className="skill-packages-conflicts">{error}</p> : null}
      {loading ? (
        <p className="runtime-v2-trajectory-muted">正在扫描…</p>
      ) : items.length === 0 ? (
        <p className="runtime-v2-trajectory-muted">
          暂无轨迹包（仅失败的运行会自动导出；显式导出走 CLI）。
        </p>
      ) : (
        <ul className="runtime-v2-trajectory-list">
          {items.map((item) => {
            const meta = metaByRun[item.runId];
            return (
              <li key={item.runId}>
                <button onClick={() => void openBundle(item.runId)} type="button">
                  <span className="runtime-v2-trajectory-run">{item.runId}</span>
                  <span className="runtime-v2-trajectory-muted">
                    {item.files.length} 个文件
                  </span>
                </button>
                {opened === item.runId && meta ? (
                  <div className="runtime-v2-trajectory-meta">
                    <pre>
                      {JSON.stringify(
                        {
                          schemaVersion: meta.manifest.schemaVersion,
                          provider: meta.manifest.provider,
                          model: meta.manifest.model,
                          files: meta.fileNames.map((name) => {
                            const file = item.files.find((f) => f.name === name);
                            return `${name}${file ? ` (${readFileSize(file.size)})` : ""}`;
                          }),
                        },
                        null,
                        2,
                      )}
                    </pre>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
