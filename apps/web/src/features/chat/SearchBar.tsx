type SearchBarProps = {
  current: number;
  hitCount: number;
  onClose: () => void;
  onNext: () => void;
  onPrev: () => void;
  onQueryChange: (value: string) => void;
  query: string;
};

export function SearchBar({
  current,
  hitCount,
  onClose,
  onNext,
  onPrev,
  onQueryChange,
  query,
}: SearchBarProps) {
  return (
    <div className="search-bar" role="search">
      <span aria-hidden="true" className="search-glyph">
        ⌕
      </span>
      <input
        aria-label="搜索当前会话"
        autoFocus
        onChange={(event) => onQueryChange(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            if (event.shiftKey) {
              onPrev();
            } else {
              onNext();
            }
          }
          if (event.key === "Escape") {
            event.preventDefault();
            onClose();
          }
        }}
        placeholder="搜索当前会话"
        value={query}
      />
      <span className="search-count">
        {hitCount > 0 ? `${current}/${hitCount}` : "0/0"}
      </span>
      <button
        aria-label="上一个匹配"
        disabled={hitCount === 0}
        onClick={onPrev}
        type="button"
      >
        ↑
      </button>
      <button
        aria-label="下一个匹配"
        disabled={hitCount === 0}
        onClick={onNext}
        type="button"
      >
        ↓
      </button>
      <button aria-label="关闭搜索" onClick={onClose} type="button">
        ×
      </button>
    </div>
  );
}
