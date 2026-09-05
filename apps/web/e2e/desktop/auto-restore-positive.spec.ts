import { expect, test } from "@playwright/test";

// G1 ② 正向（fixme：live 路径已由 SSE 投影修复覆盖——run_auto_restored→run.auto_restored
// 会推给打开中的会话；本用例的“重开历史会话回填显示”依赖事件回填/场景 harness，见 13 §12）。
test.fixme(
  "G1 ② 正向：失败 run 显示已自动回滚提示（fixture，回填待场景 harness）",
  async () => {
    expect(true).toBeTruthy();
  },
);
