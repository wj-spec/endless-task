import type { RuntimeV2ProductEvent } from "./apiTypes";

/**
 * 消息流顶部的提示条：错误条 + "已自动回滚"通知。
 *
 * 从 `ChatWorkSurface.tsx` 搬出。这两块此前夹在 header 与消息流之间，且"已自动回滚"
 * 取的是**倒序第一条**（`[...events].reverse().find(...)`）——这种细节藏在 1 400 行
 * 组件里很容易被后续改动破坏，搬出后连"取最近一条"这条规则一起直测。
 *
 * 行为零改动：文案与 `role="alert"` / `role="status"` 逐字保持；错误条的关闭按钮文案
 * 与回调不变。
 */
type StreamNoticesProps = {
  error: string | null;
  runtimeEvents?: RuntimeV2ProductEvent[];
  onDismissError: () => void;
};

/** 最近一次"自动回滚"事件的载荷（没有则为 null）。 */
export const latestAutoRestoreNotice = (
  runtimeEvents?: RuntimeV2ProductEvent[],
): Record<string, unknown> | null => {
  const event = [...(runtimeEvents ?? [])]
    .reverse()
    .find(
      (item) =>
        item.type === "run.auto_restored" ||
        item.type === "run_auto_restored",
    );
  return event && typeof event.data === "object"
    ? (event.data as Record<string, unknown>)
    : null;
};

export function StreamNotices({
  error,
  runtimeEvents,
  onDismissError,
}: StreamNoticesProps) {
  const autoRestoreNotice = latestAutoRestoreNotice(runtimeEvents);
  return (
    <>
      {error ? (
        <div className="inline-error" role="alert">
          <span>{error}</span>
          <button onClick={onDismissError} type="button">关闭</button>
        </div>
      ) : null}

      {autoRestoreNotice ? (
        <div className="inline-notice" role="status">
          <strong>已自动回滚</strong>
          <span>
            本次失败的改动已自动恢复（{String(autoRestoreNotice.restored ?? "?")}
            {" "}个文件恢复，{String(autoRestoreNotice.skipped ?? "?")}
            {" "}个跳过）；你的修改未受影响。
          </span>
        </div>
      ) : null}
    </>
  );
}
