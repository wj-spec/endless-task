import type { ReactNode } from "react";
import {
  barRatios,
  formatMetricValue,
  parseChartSpec,
  parseMetricsSpec,
  parseStepsSpec,
  type ChartSpec,
  type MetricsSpec,
  type StepsSpec,
} from "./structuredBlocks";

type StructuredBlockProps = {
  language: string;
  code: string;
  /** 解析失败时的回退渲染（通常是普通代码块）。 */
  fallback: ReactNode;
  onOpenInPanel?: () => void;
};

const BlockShell = ({
  title,
  kind,
  children,
  onOpenInPanel,
}: {
  title: string;
  kind: string;
  children: ReactNode;
  onOpenInPanel?: () => void;
}) => (
  <section className="structured-block" data-kind={kind}>
    <header className="structured-block-head">
      <span className="structured-block-kind">{kind}</span>
      {title ? <span className="structured-block-title">{title}</span> : null}
      {onOpenInPanel ? (
        <button
          className="structured-block-action"
          onClick={onOpenInPanel}
          type="button"
        >
          在右侧打开
        </button>
      ) : null}
    </header>
    <div className="structured-block-body">{children}</div>
  </section>
);

const ChartBlock = ({ spec }: { spec: ChartSpec }) => {
  const ratios = barRatios(spec.series);
  return (
    <ul className="structured-chart">
      {spec.series.map((point, index) => (
        <li key={`${point.label}-${index}`}>
          <span className="structured-chart-label" title={point.label}>
            {point.label}
          </span>
          <span className="structured-chart-track">
            <span
              className="structured-chart-bar"
              style={{ width: `${Math.max(2, ratios[index] * 100)}%` }}
            />
          </span>
          <span className="structured-chart-value">
            {formatMetricValue(point.value)}
            {spec.unit ? ` ${spec.unit}` : ""}
          </span>
        </li>
      ))}
    </ul>
  );
};

const MetricsBlock = ({ spec }: { spec: MetricsSpec }) => (
  <ul className="structured-metrics">
    {spec.items.map((item, index) => (
      <li key={`${item.label}-${index}`}>
        <span className="structured-metric-value">{item.value}</span>
        <span className="structured-metric-label">{item.label}</span>
        {item.hint ? (
          <span className="structured-metric-hint">{item.hint}</span>
        ) : null}
      </li>
    ))}
  </ul>
);

const StepsBlock = ({ spec }: { spec: StepsSpec }) => (
  <ol className="structured-steps">
    {spec.steps.map((step, index) => (
      <li
        className={step.done ? "is-done" : undefined}
        key={`${step.title}-${index}`}
      >
        <span className="structured-step-mark">{step.done ? "✓" : "○"}</span>
        <span className="structured-step-title">{step.title}</span>
        {step.detail ? (
          <span className="structured-step-detail">{step.detail}</span>
        ) : null}
      </li>
    ))}
  </ol>
);

/**
 * A7 内容类型适配器：结构化围栏块 → 专用组件；解析失败一律回退普通代码块。
 *
 * 支持 `chart`（条形对比）/`metrics`（指标卡）/`steps`（步骤清单）。
 * 不引入图表库：条形用纯 CSS 宽度表达，包体积零增长。
 */
export const StructuredBlock = ({
  language,
  code,
  fallback,
  onOpenInPanel,
}: StructuredBlockProps) => {
  if (language === "chart") {
    const spec = parseChartSpec(code);
    if (spec) {
      return (
        <BlockShell
          kind="图表"
          onOpenInPanel={onOpenInPanel}
          title={spec.title}
        >
          <ChartBlock spec={spec} />
        </BlockShell>
      );
    }
  }
  if (language === "metrics") {
    const spec = parseMetricsSpec(code);
    if (spec) {
      return (
        <BlockShell
          kind="指标"
          onOpenInPanel={onOpenInPanel}
          title={spec.title}
        >
          <MetricsBlock spec={spec} />
        </BlockShell>
      );
    }
  }
  if (language === "steps") {
    const spec = parseStepsSpec(code);
    if (spec) {
      return (
        <BlockShell
          kind="步骤"
          onOpenInPanel={onOpenInPanel}
          title={spec.title}
        >
          <StepsBlock spec={spec} />
        </BlockShell>
      );
    }
  }
  return <>{fallback}</>;
};
