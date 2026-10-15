/** N1③：「变更」视图 —— Agent 在本会话改了什么。
 *
 * 数据来自 `GET /conversations/{id}/changes`（undo 日志的 before × 当前文件的
 * unified diff，服务端 difflib 计算）。渲染复用 `CodeBlock` 的 hljs `diff`
 * 语言 —— 零新依赖（§0.3-5 的选型落定：先零依赖顶版，侧边并排等需要出现时
 * 再评估 `react-diff-viewer-continued`）。
 */

import { useEffect, useState } from "react";

import { chatApi } from "../../chat/api";
import type { ConversationChangeItem } from "../../chat/apiTypes";
import { CodeBlock } from "../../chat/CodeBlock";

export type ChangesViewProps = {
  conversationId: string;
};

const KIND_LABELS: Record<string, string> = {
  file_write: "写入",
  file_delete: "删除",
};

export function ChangesView({ conversationId }: ChangesViewProps) {
  const [items, setItems] = useState<ConversationChangeItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setItems(null);
    setError(null);
    chatApi
      .listConversationChanges(conversationId)
      .then((response) => {
        if (!cancelled) {
          setItems(response.items);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setError("变更清单加载失败，请稍后重试。");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [conversationId]);

  return (
    <section aria-label="变更" className="changes-view">
      {error !== null ? (
        <p className="changes-note">{error}</p>
      ) : items === null ? (
        <p className="changes-note">加载中…</p>
      ) : items.length === 0 ? (
        <p className="changes-note">本会话还没有文件变更。</p>
      ) : (
        <ul className="changes-list">
          {items.map((item) => (
            <li className="changes-item" key={item.id}>
              <header className="changes-item-header">
                <span className="changes-path">{item.target}</span>
                <span className="changes-kind">
                  {KIND_LABELS[item.kind] ?? item.kind}
                  {item.status === "undone" ? " · 已撤销" : ""}
                </span>
              </header>
              {item.diff === null ? (
                <p className="changes-note">
                  {item.beforeAvailable
                    ? "内容无变化。"
                    : "变更前内容不可用（二进制或过大），无法对比。"}
                </p>
              ) : (
                <CodeBlock code={item.diff} language="diff" />
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
