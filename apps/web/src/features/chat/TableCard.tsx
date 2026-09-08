import { useRef, useState, type ReactNode } from "react";

type TableCardProps = {
  children: ReactNode;
  /** 行数（含表头），用于判断是否值得用卡片承载。 */
  rowCount?: number;
};

/**
 * A7：表格卡片。宽表在聊天里容易挤成一团，这里给出横向滚动 + 一键复制。
 *
 * 复制的是表格的纯文本（制表符分隔），可直接粘进表格软件。
 */
export const TableCard = ({ children, rowCount = 0 }: TableCardProps) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);

  const copyTable = async () => {
    const table = containerRef.current?.querySelector("table");
    if (!table) return;
    const rows = Array.from(table.querySelectorAll("tr")).map((row) =>
      Array.from(row.querySelectorAll("th,td"))
        .map((cell) => (cell.textContent ?? "").trim())
        .join("\t"),
    );
    const text = rows.join("\n");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="table-card" data-rows={rowCount}>
      <div className="table-card-head">
        <span className="table-card-label">表格</span>
        <button className="table-card-copy" onClick={() => void copyTable()} type="button">
          {copied ? "已复制" : "复制表格"}
        </button>
      </div>
      <div className="table-card-scroll" ref={containerRef}>
        <table>{children}</table>
      </div>
    </div>
  );
};
