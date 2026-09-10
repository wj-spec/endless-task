/**
 * 浮层（portal 到 body 的菜单/弹窗）的标记与判定。
 *
 * 为什么需要：弹出层的「点外部关闭」监听挂在 `document` 上，而嵌套的子菜单
 * 是 **portal 到 body** 的——它不在父弹层的 DOM 子树里。父层因此把「点子菜单」
 * 误判成「点外部」，在 **mousedown 阶段**就把子菜单卸载掉，浏览器接着不会再
 * 派发 `mouseup`/`click` 给那个已经不存在的按钮：菜单项点了没反应。
 * （分支导航器的「右侧对照 / 设为主线 / 重命名 / 归档分支」就是这么失效的。）
 *
 * 约定：所有 portal 浮层的根节点都带上 `data-floating-layer`，
 * 外部点击判定先问 `isInsideFloatingLayer`，再判断"是否在自己子树里"。
 */

export const FLOATING_LAYER_ATTRIBUTE = "data-floating-layer";

/** 给浮层根节点用的属性（`<div {...floatingLayerProps("row-menu")}>`）。 */
export const floatingLayerProps = (name: string) => ({
  [FLOATING_LAYER_ATTRIBUTE]: name,
});

export const isInsideFloatingLayer = (target: EventTarget | null): boolean => {
  if (!(target instanceof Element)) return false;
  return target.closest(`[${FLOATING_LAYER_ATTRIBUTE}]`) !== null;
};
