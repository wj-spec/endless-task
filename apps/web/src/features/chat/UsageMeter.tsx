import type { RuntimeV2UsageSummary } from "./apiTypes";
import { usageLine, usageTooltip } from "./usageCost";

type UsageMeterProps = {
  usage: RuntimeV2UsageSummary;
};

/**
 * C5 成本/延迟可见：一行轻量用量摘要（默认不打扰，hover 看定价口径）。
 *
 * 成本是估算；未定价模型显示"未定价"而不是编造数字。
 */
export const UsageMeter = ({ usage }: UsageMeterProps) => (
  <div
    aria-label="本轮用量"
    className={`usage-meter${usage.costPriced ? "" : " usage-meter-unpriced"}`}
    title={usageTooltip(usage)}
  >
    <span className="usage-meter-label">本轮</span>
    <span className="usage-meter-value">{usageLine(usage)}</span>
    {usage.unpricedTurns > 0 && usage.costPriced ? (
      <span className="usage-meter-note">{usage.unpricedTurns} 轮未定价</span>
    ) : null}
  </div>
);
