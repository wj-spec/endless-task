import {
  Children,
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
} from "react";

const DEFAULT_MAIN_PERCENT = 60;
const MIN_MAIN_PERCENT = 38;
const MAX_MAIN_PERCENT = 72;
const STORAGE_KEY = "endless-task-branch-split-percent";

type SplitStyle = CSSProperties & {
  "--branch-main-size": string;
  "--branch-side-size": string;
};

type ResizableChatSplitProps = {
  children: ReactNode;
  sideOpen: boolean;
};

const clampMainPercent = (value: number) =>
  Math.min(MAX_MAIN_PERCENT, Math.max(MIN_MAIN_PERCENT, value));

const readInitialPercent = () => {
  if (typeof window === "undefined") return DEFAULT_MAIN_PERCENT;
  const stored = Number.parseFloat(localStorage.getItem(STORAGE_KEY) ?? "");
  return Number.isFinite(stored)
    ? clampMainPercent(stored)
    : DEFAULT_MAIN_PERCENT;
};

export function ResizableChatSplit({
  children,
  sideOpen,
}: ResizableChatSplitProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  const [mainPercent, setMainPercent] = useState(readInitialPercent);
  const [resizing, setResizing] = useState(false);
  const nodes = Children.toArray(children);
  const mainSurface = nodes[0] ?? null;
  const sideSurface = nodes[1] ?? null;

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, String(mainPercent));
  }, [mainPercent]);

  const updateFromPointer = (clientX: number) => {
    const bounds = containerRef.current?.getBoundingClientRect();
    if (!bounds || bounds.width <= 0) return;
    const dividerWidth = 12;
    const availableWidth = Math.max(1, bounds.width - dividerWidth);
    const offset = clientX - bounds.left - dividerWidth / 2;
    setMainPercent(clampMainPercent((offset / availableWidth) * 100));
  };

  const finishPointerResize = (event: PointerEvent<HTMLDivElement>) => {
    draggingRef.current = false;
    setResizing(false);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.shiftKey ? 5 : 2;
    let next = mainPercent;
    if (event.key === "ArrowLeft") next -= step;
    else if (event.key === "ArrowRight") next += step;
    else if (event.key === "Home") next = MIN_MAIN_PERCENT;
    else if (event.key === "End") next = MAX_MAIN_PERCENT;
    else if (event.key === "Enter") next = DEFAULT_MAIN_PERCENT;
    else return;
    event.preventDefault();
    setMainPercent(clampMainPercent(next));
  };

  const splitStyle: SplitStyle = {
    "--branch-main-size": `${mainPercent}fr`,
    "--branch-side-size": `${100 - mainPercent}fr`,
  };

  return (
    <div
      className={`chat-split${sideOpen ? " is-side-open" : ""}${resizing ? " is-resizing" : ""}`}
      ref={containerRef}
      style={splitStyle}
    >
      {mainSurface}
      {sideOpen && sideSurface ? (
        <>
          <div
            aria-label="调整主会话与分支宽度"
            aria-orientation="vertical"
            aria-valuemax={MAX_MAIN_PERCENT}
            aria-valuemin={MIN_MAIN_PERCENT}
            aria-valuenow={Math.round(mainPercent)}
            aria-valuetext={`主会话 ${Math.round(mainPercent)}%，分支 ${Math.round(100 - mainPercent)}%`}
            className="chat-split-resizer"
            onDoubleClick={() => setMainPercent(DEFAULT_MAIN_PERCENT)}
            onKeyDown={handleKeyDown}
            onLostPointerCapture={() => {
              draggingRef.current = false;
              setResizing(false);
            }}
            onPointerCancel={finishPointerResize}
            onPointerDown={(event) => {
              if (event.pointerType === "mouse" && event.button !== 0) return;
              draggingRef.current = true;
              setResizing(true);
              event.currentTarget.setPointerCapture(event.pointerId);
              updateFromPointer(event.clientX);
            }}
            onPointerMove={(event) => {
              if (!draggingRef.current) return;
              updateFromPointer(event.clientX);
            }}
            onPointerUp={finishPointerResize}
            role="separator"
            tabIndex={0}
            title="拖动调整宽度，双击恢复 6:4"
          >
            <span aria-hidden="true" />
          </div>
          {sideSurface}
        </>
      ) : null}
    </div>
  );
}