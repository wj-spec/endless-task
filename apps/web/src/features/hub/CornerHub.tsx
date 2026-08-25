import type { PermissionMode } from "../chat/apiTypes";
import { RowMenu } from "../ui/RowMenu";
import type { RowMenuItem } from "../ui/RowMenu";

const PERMISSION_LABEL: Record<PermissionMode, string> = {
  confirm_every_time: "逐项确认",
  trust_local_writes: "本地写已信任",
  trust_all: "全部已信任",
};

type CornerHubProps = {
  onOpenAssistant: () => void;
  onOpenSettings: () => void;
  onOpenWorkspace: () => void;
  pendingTotal: number;
  permissionMode: PermissionMode | null;
  workspaceAvailable: boolean;
};

export function CornerHub({
  onOpenAssistant,
  onOpenSettings,
  onOpenWorkspace,
  pendingTotal,
  permissionMode,
  workspaceAvailable,
}: CornerHubProps) {
  const items: RowMenuItem[] = [
    {
      label:
        pendingTotal > 0 ? `助手面板（${pendingTotal} 待处理）` : "助手面板",
      onSelect: onOpenAssistant,
    },
  ];
  if (workspaceAvailable) {
    items.push({
      className: "hub-workspace-item",
      label: "工作区",
      onSelect: onOpenWorkspace,
    });
  }
  items.push({ label: "设置", onSelect: onOpenSettings });
  if (permissionMode) {
    items.push({
      label: `权限：${PERMISSION_LABEL[permissionMode]}`,
      onSelect: onOpenSettings,
    });
  }
  return (
    <div className="corner-hub">
      <RowMenu
        className="hub-menu"
        items={items}
        placement="up"
        trigger={
          <>
            <span aria-hidden="true">∞</span>
            {pendingTotal > 0 ? <span className="hub-dot" /> : null}
          </>
        }
        triggerAriaLabel={
          pendingTotal > 0
            ? `助手菜单，${pendingTotal} 条待处理`
            : "助手菜单"
        }
        triggerClassName="corner-hub-button"
      />
    </div>
  );
}
