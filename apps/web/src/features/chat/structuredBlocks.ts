/**
 * A7 内容类型适配：把结构化文本块解析成可渲染的数据。
 *
 * 约定（模型在回答里用带语言标记的围栏代码块给出结构化结果）：
 *
 * ```chart
 * {"title": "指标对比", "unit": "KB", "series": [{"label": "chat1.md", "value": 78}]}
 * ```
 *
 * ```metrics
 * {"items": [{"label": "总大小", "value": "97KB"}]}
 * ```
 *
 * ```steps
 * {"steps": [{"title": "读取文件"}, {"title": "汇总指标"}]}
 * ```
 *
 * 解析一律**严格 + 容错**：字段缺失或类型不对就返回 null，调用方回退到普通
 * 代码块渲染，绝不把坏数据画成错误的图。
 */

export type ChartSpec = {
  title: string;
  unit: string;
  series: Array<{ label: string; value: number }>;
};

export type MetricsSpec = {
  title: string;
  items: Array<{ label: string; value: string; hint?: string }>;
};

export type StepsSpec = {
  title: string;
  steps: Array<{ title: string; detail?: string; done?: boolean }>;
};

export const STRUCTURED_LANGUAGES = ["chart", "metrics", "steps"] as const;

export type StructuredLanguage = (typeof STRUCTURED_LANGUAGES)[number];

export const isStructuredLanguage = (
  language: string,
): language is StructuredLanguage =>
  (STRUCTURED_LANGUAGES as readonly string[]).includes(language);

const asRecord = (value: unknown): Record<string, unknown> | null =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;

const asString = (value: unknown): string =>
  typeof value === "string" ? value.trim() : "";

const asNumber = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;

const parseJson = (raw: string): unknown => {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
};

export const parseChartSpec = (raw: string): ChartSpec | null => {
  const payload = asRecord(parseJson(raw));
  if (!payload) return null;
  const series = Array.isArray(payload.series) ? payload.series : null;
  if (!series) return null;
  const points: ChartSpec["series"] = [];
  for (const item of series) {
    const entry = asRecord(item);
    if (!entry) continue;
    const label = asString(entry.label);
    const value = asNumber(entry.value);
    if (!label || value === null) continue;
    points.push({ label, value });
  }
  if (points.length === 0) return null;
  return {
    title: asString(payload.title),
    unit: asString(payload.unit),
    series: points,
  };
};

export const parseMetricsSpec = (raw: string): MetricsSpec | null => {
  const payload = asRecord(parseJson(raw));
  if (!payload) return null;
  const items = Array.isArray(payload.items) ? payload.items : null;
  if (!items) return null;
  const parsed: MetricsSpec["items"] = [];
  for (const item of items) {
    const entry = asRecord(item);
    if (!entry) continue;
    const label = asString(entry.label);
    if (!label) continue;
    const value =
      asString(entry.value) ||
      (asNumber(entry.value) !== null ? String(entry.value) : "");
    if (!value) continue;
    parsed.push({ label, value, hint: asString(entry.hint) || undefined });
  }
  if (parsed.length === 0) return null;
  return { title: asString(payload.title), items: parsed };
};

export const parseStepsSpec = (raw: string): StepsSpec | null => {
  const payload = asRecord(parseJson(raw));
  if (!payload) return null;
  const steps = Array.isArray(payload.steps) ? payload.steps : null;
  if (!steps) return null;
  const parsed: StepsSpec["steps"] = [];
  for (const item of steps) {
    const entry = asRecord(item);
    if (!entry) continue;
    const title = asString(entry.title);
    if (!title) continue;
    parsed.push({
      title,
      detail: asString(entry.detail) || undefined,
      done: entry.done === true,
    });
  }
  if (parsed.length === 0) return null;
  return { title: asString(payload.title), steps: parsed };
};

/** 图表条的相对长度（最大值为 1，全零时统一给 0）。 */
export const barRatios = (series: ChartSpec["series"]): number[] => {
  const max = Math.max(...series.map((point) => point.value), 0);
  if (max <= 0) return series.map(() => 0);
  return series.map((point) => Math.max(0, point.value) / max);
};

/** 数值格式化：大数用 K/M 缩写，小数保留两位。 */
export const formatMetricValue = (value: number): string => {
  const absolute = Math.abs(value);
  if (absolute >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (absolute >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  if (Number.isInteger(value)) return String(value);
  return value.toFixed(2);
};

/** 表格是否值得用卡片渲染（列数或行数达到阈值）。 */
export const isWideTable = (columns: number, rows: number): boolean =>
  columns >= 3 || rows >= 5;
