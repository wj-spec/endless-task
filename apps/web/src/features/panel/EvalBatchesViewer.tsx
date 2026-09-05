import { useCallback, useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import { RefreshIcon } from "../ui/Icons";
import type { EvalBatchDetail, EvalBatchSummary } from "../chat/apiTypes";

const formatAggregate = (value: Record<string, unknown> | null): string =>
  value ? JSON.stringify(value) : "（无聚合）";

/** 开发/诊断向：离线 eval 批次只读查看（P2-1c/d）。 */
export function EvalBatchesViewer() {
  const [items, setItems] = useState<EvalBatchSummary[]>([]);
  const [details, setDetails] = useState<Record<string, EvalBatchDetail>>({});
  const [error, setError] = useState<string | null>(null);
  const [opened, setOpened] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setItems(await chatApi.listEvalBatches());
      setError(null);
    } catch {
      setError("无法加载评测批次，请重试。");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openBatch = async (id: string) => {
    if (opened === id) {
      setOpened(null);
      return;
    }
    setOpened(id);
    if (details[id]) return;
    try {
      const detail = await chatApi.getEvalBatch(id);
      setDetails((current) => ({ ...current, [id]: detail }));
    } catch {
      setError("无法读取评测批次详情，请重试。");
    }
  };

  return (
    <section aria-label="评测批次" className="runtime-v2-trajectory">
      <div className="runtime-v2-trajectory-head">
        <strong>评测批次（诊断）</strong>
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
      {items.length === 0 ? (
        <p className="runtime-v2-trajectory-muted">
          暂无评测批次（离线 eval 走 CLI/门禁，这里只读查看结果）。
        </p>
      ) : (
        <ul className="runtime-v2-trajectory-list">
          {items.map((item) => {
            const detail = details[item.id];
            return (
              <li key={item.id}>
                <button onClick={() => void openBatch(item.id)} type="button">
                  <span className="runtime-v2-trajectory-run">{item.id}</span>
                  <span className="runtime-v2-trajectory-muted">
                    {item.mode} · {item.status} · {item.runCount} runs
                  </span>
                </button>
                {opened === item.id && detail ? (
                  <div className="runtime-v2-trajectory-meta">
                    <pre>
                      {JSON.stringify(
                        {
                          mode: detail.mode,
                          status: detail.status,
                          runCount: detail.runCount,
                          resultCount: detail.resultCount,
                          judge: detail.judgeProvider ?? null,
                          aggregate: formatAggregate(detail.aggregate),
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
