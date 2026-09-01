import { rmSync, readdirSync } from "node:fs";
import { join } from "node:path";

/**
 * Playwright globalTeardown：清理 e2e 运行期间创建的临时工作区目录。
 *
 * `e2e/support/api.ts` 的 tempDir() 会在 `${ENDLESS_TASK_E2E_TMPDIR ?? "/tmp"}`
 * 下创建 `endless-e2e-*` 目录作为工作区根；每次运行后清理，避免在 /tmp 累积。
 * 这里只删除本 E2E 约定的前缀目录，绝不触碰其它目录。
 */
async function globalTeardown(): Promise<void> {
  const base = process.env.ENDLESS_TASK_E2E_TMPDIR ?? "/tmp";
  let entries: string[] = [];
  try {
    entries = readdirSync(base);
  } catch {
    return;
  }
  for (const name of entries) {
    if (name.startsWith("endless-e2e-")) {
      try {
        rmSync(join(base, name), { recursive: true, force: true });
      } catch {
        // 某个目录被占用/权限不足时跳过，不阻断收尾。
      }
    }
  }
}

export default globalTeardown;
