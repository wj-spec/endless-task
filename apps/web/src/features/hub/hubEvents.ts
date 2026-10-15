/**
 * 切片 10 T4（item 3）：hub 事件的**页面内广播**。
 *
 * 背景：「已安排」面板此前只在挂载时 `load()` 一次，后端到点触发的任务/运行状态变化
 * 在面板上永远不刷新。让面板自己再开一条 SSE 会变成同一页面两条 hub 流；这里改成
 * 复用既有那条：`useAssistantHub`（`App.tsx` 里唯一的一条 hub 流）收到事件后广播，
 * 页面内其它订阅者按事件类型决定要不要重拉自己的数据。
 *
 * 只广播**事件类型**（订阅者自己决定拉什么数据），不搬运载荷：类型已经够用，且避免
 * 把 hub 载荷的形状固化成第二个契约。
 *
 * 本模块是纯内存 pub/sub，没有新增依赖、没有全局副作用（订阅必须成对取消）。
 */
export type HubEventListener = (type: string) => void;

const listeners = new Set<HubEventListener>();

/** 订阅 hub 事件类型；返回取消订阅函数（组件卸载时必须调用）。 */
export const subscribeHubEvents = (listener: HubEventListener): (() => void) => {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
};

/** 广播一个 hub 事件类型给当前所有订阅者。 */
export const publishHubEvent = (type: string): void => {
  // 复制一份再遍历：订阅者在回调里取消订阅（或订阅新的）不会打断本轮广播。
  for (const listener of [...listeners]) {
    listener(type);
  }
};

/** 当前订阅者数量（测试用：用来钉住"卸载后不再被通知"）。 */
export const hubEventSubscriberCount = (): number => listeners.size;
