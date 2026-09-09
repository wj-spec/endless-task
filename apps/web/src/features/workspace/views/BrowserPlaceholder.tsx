import { GlobeIcon } from "../../ui/Icons";

export type BrowserPlaceholderProps = {
  onOpenExternal?: () => void;
};

/**
 * S10：浏览器视图占位。
 *
 * 浏览器能力（只读抓取 / 引用入库 / 内嵌渲染）在
 * `docs/product-improvements/PERSONAL-WORKBENCH-CAPABILITIES.md` §5 有独立路线，
 * 这里先给出占位与"用系统浏览器打开"的降级入口，避免为了一个标签推迟整体重构。
 */
export function BrowserPlaceholder({ onOpenExternal }: BrowserPlaceholderProps) {
  return (
    <div className="browser-placeholder">
      <GlobeIcon className="browser-placeholder-icon" size={28} />
      <h3>浏览器视图规划中</h3>
      <p>
        后续会在这里提供只读网页抓取（正文 + 引用编号）、页面快照与"加入知识库"。
        当前可先用系统浏览器打开链接，或让对话里的引用跳转。
      </p>
      {onOpenExternal ? (
        <button className="file-viewer-action" onClick={onOpenExternal} type="button">
          用系统浏览器打开当前工作区说明
        </button>
      ) : null}
    </div>
  );
}
