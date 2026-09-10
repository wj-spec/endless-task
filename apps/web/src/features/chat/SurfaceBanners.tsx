import type { RuntimeV2Lane } from "./apiTypes";

/**
 * 主/侧工作面的"横幅"区：临时会话 / 正在查看分支 / 其他分支正在运行。
 *
 * 从 `ChatWorkSurface.tsx`（1 611 行）的 JSX 里整块搬出——这三块各占十几行，夹在
 * header 与消息流之间很难一眼看出"什么时候会显示、点了会怎样"。搬出后可以直测三
 * 种可见性组合（此前只能靠 e2e 顺带覆盖）。
 *
 * 行为零改动：可见性判断沿用**原表达式的等价布尔量**（`pendingAction !== null` 的
 * 结果由调用方算好传 `pending`，`runtimeSnapshot?.runningLaneId` 同理传
 * `runningLaneId`），文案、`aria-*`、按钮禁用态逐字保持原样。
 */
export type SurfaceBannersProps = {
  ephemeral: boolean;
  parentTitle: string | null;
  viewingBranch: boolean;
  currentLaneLabel: string;
  currentLane: RuntimeV2Lane | null;
  mainLaneId: string | null;
  otherLaneRunning: boolean;
  runningLaneLabel: string;
  runningLaneId: string | null;
  runningRunId: string | null;
  pending: boolean;
  canOpenRunningLane: boolean;
  canCancelRunningRun: boolean;
  canSwitchToMain: boolean;
  onPromote: () => void;
  onSwitchLane?: (laneId: string) => void;
  onPromoteLane?: (laneId: string) => void;
  onOpenRunningLane?: (laneId: string) => void;
  onCancelRunningRun?: (runId: string) => void;
};

export function SurfaceBanners({
  ephemeral,
  parentTitle,
  viewingBranch,
  currentLaneLabel,
  currentLane,
  mainLaneId,
  otherLaneRunning,
  runningLaneLabel,
  runningLaneId,
  runningRunId,
  pending,
  canOpenRunningLane,
  canCancelRunningRun,
  canSwitchToMain,
  onPromote,
  onSwitchLane,
  onPromoteLane,
  onOpenRunningLane,
  onCancelRunningRun,
}: SurfaceBannersProps) {
  return (
    <>
      {ephemeral ? (
        <div className="branch-banner" role="note">
          <span className="branch-banner-text">
            临时会话
            {parentTitle
              ? ` · 源自《${parentTitle}》`
              : ""}
            {" "}· 不写入记忆，用完可丢弃
          </span>
          <button
            disabled={pending}
            onClick={onPromote}
            type="button"
          >
            升级为正式
          </button>
        </div>
      ) : null}

      {viewingBranch ? (
        <div className="branch-banner" role="note">
          <span className="branch-banner-text">
            <span aria-hidden="true" className="branch-banner-glyph">⑂</span>
            <span
              className="branch-banner-label"
              title={
                currentLane?.displayName ?? currentLane?.title ?? currentLane?.summary ?? ""
              }
            >
              正在查看分支「{currentLaneLabel}」
            </span>
            <span className="branch-banner-hint">查看不会改变主线。</span>
          </span>
          <div className="branch-banner-actions">
            {canSwitchToMain && mainLaneId && onSwitchLane ? (
              <button
                disabled={pending}
                onClick={() => onSwitchLane(mainLaneId)}
                type="button"
              >
                返回主线
              </button>
            ) : null}
            {currentLane && onPromoteLane ? (
              <button
                disabled={pending}
                onClick={() => onPromoteLane(currentLane.id)}
                type="button"
              >
                设为主线
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      {otherLaneRunning ? (
        <div
          aria-atomic="true"
          aria-live="polite"
          className="branch-banner runtime-conflict-banner"
          role="status"
        >
          <span className="branch-banner-text">
            「{runningLaneLabel}」正在运行；同一会话暂不支持多分支并行。
          </span>
          <div className="branch-banner-actions">
            {runningLaneId && canOpenRunningLane && onOpenRunningLane ? (
              <button
                disabled={pending}
                onClick={() => onOpenRunningLane(runningLaneId)}
                type="button"
              >
                查看运行位置
              </button>
            ) : null}
            {runningRunId && canCancelRunningRun && onCancelRunningRun ? (
              <button
                disabled={pending}
                onClick={() => onCancelRunningRun(runningRunId)}
                type="button"
              >
                停止后继续
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  );
}
