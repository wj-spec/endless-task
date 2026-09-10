/**
 * 继承内容的摘要条与分隔线（从 `ChatWorkSurface.tsx` 搬出，行为零改动）。
 *
 * 临时会话会继承来源会话的历史：摘要条负责说明"继承了多少轮、来自哪个会话"并切换
 * 是否展开，分隔线负责标出"继承内容到哪里为止"。两条分隔线的文案不同（一条在列表
 * 中间、一条在末尾且是"全部继承"），所以拆成两个组件而不是一个带 flag 的组件。
 */
export function LineageSummary({
  parentTitle,
  inheritedTurnCount,
  open,
  onToggle,
}: {
  parentTitle: string | null | undefined;
  inheritedTurnCount: number;
  open: boolean;
  onToggle: () => void;
}) {
  return (
    <section className="lineage-summary" role="note">
      <div>
        <strong>继承自《{parentTitle ?? "主会话"}》</strong>
        <span>{inheritedTurnCount} 轮上下文已随临时会话复制并隔离保存</span>
      </div>
      <button aria-expanded={open} onClick={onToggle} type="button">
        {open ? "收起继承内容" : "查看继承内容"}
      </button>
    </section>
  );
}

/** 列表中段的分隔线（某一条之后就是本会话内容）。 */
export function LineageDivider({
  parentTitle,
}: {
  parentTitle: string | null | undefined;
}) {
  return (
    <div className="lineage-divider" role="note">
      以上继承自《{parentTitle ?? "主会话"}》，以下是本会话内容
    </div>
  );
}

/** 列表末尾的分隔线（所有轮次都是继承来的）。 */
export function LineageTailDivider({
  parentTitle,
}: {
  parentTitle: string | null | undefined;
}) {
  return (
    <div className="lineage-divider" role="note">
      以上全部继承自《{parentTitle ?? "主会话"}》，从这里开始是新内容
    </div>
  );
}
