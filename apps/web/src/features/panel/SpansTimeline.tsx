import { useCallback, useEffect, useState } from "react";
import { chatApi } from "../chat/api";
import type { RuntimeV2Span } from "../chat/apiTypes";

const KIND_LABELS: Record<string, string> = {
  internal: "运行",
  model: "模型轮",
  provider: "提供方",
  tool: "工具",
  context: "上下文",
  effect: "副作用",
  child: "子代理",
};

const STATUS_LABELS: Record<string, string> = {
  completed: "完成",
  failed: "失败",
  running: "运行中",
  created: "已创建",
};

const kindLabel = (kind: string): string => KIND_LABELS[kind] ?? kind;
const statusLabel = (status: string): string => STATUS_LABELS[status] ?? status;

const depthOf = (
  spanId: string,
  spans: RuntimeV2Span[],
  cache: Map<string, number>,
): number => {
  if (cache.has(spanId)) return cache.get(spanId)!;
  const span = spans.find((item) => item.spanId === spanId);
  if (!span?.parentSpanId) return 0;
  const depth = depthOf(span.parentSpanId, spans, cache) + 1;
  cache.set(spanId, depth);
  return depth;
};

/** 诊断：run 的执行时间线（span 树，只读）。 */
export function SpansTimeline({ runId }: { runId?: string | null }) {
  const [spans, setSpans] = useState<RuntimeV2Span[]>([]);
  const [available, setAvailable] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!runId) return;
    setError(null);
    try {
      const response = await chatApi.getRuntimeV2RunSpans(runId);
      setSpans(response.spans);
      setAvailable(response.available);
    } catch {
      setError("无法读取执行轨迹，请重试。");
    }
  }, [runId]);

  useEffect(() => {
    setSpans([]);
    void load();
  }, [load]);

  const cache = new Map<string, number>();

  return (
    <section aria-label="执行轨迹" className="runtime-v2-trajectory">
      <div className="runtime-v2-trajectory-head">
        <strong>执行轨迹（span 时间线）</strong>
        {runId ? (
          <button onClick={() => void load()} type="button">
            刷新
          </button>
        ) : null}
      </div>
      {error ? <p className="skill-packages-conflicts">{error}</p> : null}
      {!runId ? (
        <p className="runtime-v2-trajectory-muted">尚无活动运行。</p>
      ) : !available ? (
        <p className="runtime-v2-trajectory-muted">轨迹记录未启用。</p>
      ) : spans.length === 0 ? (
        <p className="runtime-v2-trajectory-muted">该运行没有 span 记录。</p>
      ) : (
        <ul className="spans-timeline">
          {spans.map((span) => (
            <li
              className={`is-${span.status}`}
              key={span.spanId}
              style={{ paddingLeft: `${Math.min(depthOf(span.spanId, spans, cache), 3) * 14}px` }}
            >
              <span className="spans-kind">{kindLabel(span.kind)}</span>
              <span className="spans-name">{span.name}</span>
              <span className="spans-status">{statusLabel(span.status)}</span>
              <span className="spans-duration">
                {span.durationMs !== null ? `${span.durationMs} ms` : "—"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
