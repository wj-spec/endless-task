/**
 * 空会话与加载态（从 `ChatWorkSurface.tsx` 底部搬出，行为零改动）。
 *
 * 这两块原先挂在 800+ 行文件的末尾，属于"读了半屏才看到"的东西；搬出后可以直测
 * 三条引导语各自送出什么文案——此前只能靠 e2e 顺带覆盖。
 */
export function EmptyConversation({
  onSuggestion,
}: {
  onSuggestion: (value: string) => void;
}) {
  return (
    <section className="empty-conversation">
      <span aria-hidden="true" className="empty-symbol">∞</span>
      <h2>今天想聊些什么？</h2>
      <p>从一个问题、一个想法，或一件想理清的事开始。</p>
      <div className="prompt-suggestions">
        <button onClick={() => onSuggestion("帮我梳理一下今天最重要的三件事")} type="button">
          帮我梳理今天最重要的事
        </button>
        <button onClick={() => onSuggestion("我有一个新想法，想和你一起推敲")} type="button">
          和我一起推敲一个想法
        </button>
        <button onClick={() => onSuggestion("帮我把这段内容整理成一篇可复用的文档")} type="button">
          把内容整理成一篇可复用文档
        </button>
      </div>
      <p className="empty-hint">在任意回答底部选择「从此处分叉」，可以保留当前上下文开始一次新的尝试。</p>
    </section>
  );
}

export function LoadingState() {
  return (
    <div className="loading-state" aria-label="正在加载对话">
      <span />
      <span />
      <span />
    </div>
  );
}
