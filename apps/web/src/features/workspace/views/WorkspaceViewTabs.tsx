import { AddIcon, CloseIcon } from "../../ui/Icons";
import { RowMenu } from "../../ui/RowMenu";
import {
  VIEW_HINTS,
  VIEW_LABELS,
  VIEW_MENU,
  type WorkspaceView,
  type WorkspaceViewKind,
} from "./types";

export type WorkspaceViewTabsProps = {
  views: WorkspaceView[];
  activeKind: WorkspaceViewKind;
  onActivate: (kind: WorkspaceViewKind) => void;
  onClose: (kind: WorkspaceViewKind) => void;
  onCreate: (kind: WorkspaceViewKind) => void;
};

/**
 * S10：视图标签条。
 *
 * 参考 `dsh-desktop/deepseek-harness` 的会话视图标签：`role="tablist"` +
 * `role="tab"` + 选中态；这里额外支持关闭与「+」新建。
 */
export function WorkspaceViewTabs({
  views,
  activeKind,
  onActivate,
  onClose,
  onCreate,
}: WorkspaceViewTabsProps) {
  const openKinds = views.map((view) => view.kind);
  return (
    <div className="workspace-view-tabs" role="tablist">
      {views.map((view) => (
        <span
          className={
            view.kind === activeKind
              ? "workspace-view-tab is-active"
              : "workspace-view-tab"
          }
          key={view.kind}
        >
          <button
            aria-selected={view.kind === activeKind}
            className="workspace-view-tab-label"
            onClick={() => onActivate(view.kind)}
            role="tab"
            type="button"
          >
            {view.title}
          </button>
          {views.length > 1 ? (
            <button
              aria-label={`关闭${view.title}`}
              className="workspace-view-tab-close"
              onClick={() => onClose(view.kind)}
              type="button"
            >
              <CloseIcon size={11} />
            </button>
          ) : null}
        </span>
      ))}
      <RowMenu
        items={VIEW_MENU.map((kind) => ({
          label: openKinds.includes(kind)
            ? `${VIEW_LABELS[kind]}（已打开）`
            : VIEW_LABELS[kind],
          onSelect: () => onCreate(kind),
        }))}
        trigger={<AddIcon size={15} />}
        triggerAriaLabel="新建视图"
        triggerClassName="workspace-view-add"
      />
      <span className="workspace-view-hint">
        {VIEW_HINTS[activeKind]}
      </span>
    </div>
  );
}
