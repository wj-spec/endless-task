// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClientError, chatApi } from "../chat/api";
import type { McpCallRecord, McpServer } from "../chat/apiTypes";
import { McpContent } from "./McpManagement";

// React 19：不声明这个全局，`act(...)` 只发警告、不做真正的批处理。
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT =
  true;

/**
 * 「MCP」面板的 jsdom 覆盖。本批（F1）新增的是**编辑已有服务器**。
 *
 * 这份文件刻意**不** `vi.mock("../chat/api")`：整块换掉被测模块就测不到接线，
 * 也拿不到真的 `ApiClientError`（失败归因那几条要它）。改为 `vi.spyOn` 真模块上的
 * 方法——组件调用的就是这里被记录的那几个函数。
 *
 * 另外，fixture **不许**带 `env` / `headers`：`GET /mcp/servers` 的 `_mcp_json`
 * （`api/routes/mcp.py:23-51`）刻意不回显它们，带上去会让"读不到密钥"这条契约
 * 从用例里消失。
 */

const CALL_FIXTURE: McpCallRecord[] = [
  {
    time: "2026-09-09T10:00:00.000Z",
    operation: "mcp__demo__echo",
    detail: "server=demo; status=ok",
    durationMs: 12,
    status: "ok",
  },
  {
    time: "2026-09-09T10:01:00.000Z",
    operation: "mcp__demo__boom",
    detail: "server=demo; status=error",
    durationMs: 5,
    status: "error",
  },
];

const makeServer = (overrides: Partial<McpServer> = {}): McpServer => ({
  id: "srv_1",
  name: "demo",
  transport: "stdio",
  command: "python",
  args: ["-m", "demo"],
  cwd: "/tmp/demo",
  url: "",
  enabled: true,
  toolCallTimeoutSeconds: 60,
  state: "connected",
  toolCount: 1,
  lastError: null,
  reloadError: null,
  // F2 契约：`allowedTools === null` = 未配置 = 全部允许（迁移前/新建的默认），
  // `allowUnattended` 默认 false（新建服务器的安全默认）。
  allowedTools: null,
  allowUnattended: false,
  tools: [
    {
      publicName: "mcp__demo__echo",
      rawName: "echo",
      description: "Echo",
      effect: "read_only",
      requiresExplicitConfirmation: false,
      allowed: true,
    },
  ],
  createdAt: "2026-09-01T00:00:00.000Z",
  updatedAt: "2026-09-01T00:00:00.000Z",
  ...overrides,
});

/** 一台 HTTP 服务器：只有它才**在屏上渲染** headers 字段（stdio 那份根本不渲染）。 */
const HTTP_SERVER = makeServer({
  transport: "http",
  command: "",
  args: [],
  url: "https://old/mcp",
  cwd: "",
});

function installApiSpies(servers: McpServer[]) {
  return {
    list: vi.spyOn(chatApi, "listMcpServers").mockResolvedValue(servers),
    calls: vi.spyOn(chatApi, "listMcpCalls").mockResolvedValue(CALL_FIXTURE),
    patch: vi.spyOn(chatApi, "patchMcpServer").mockResolvedValue(servers[0]),
    reload: vi
      .spyOn(chatApi, "reloadMcpServer")
      .mockResolvedValue(servers[0]),
    create: vi
      .spyOn(chatApi, "createMcpServer")
      .mockResolvedValue(servers[0]),
    remove: vi.spyOn(chatApi, "deleteMcpServer").mockResolvedValue(undefined),
  };
}

let api: ReturnType<typeof installApiSpies>;

let container: HTMLDivElement;
let root: Root;

const flush = async () => {
  // 一条宏任务足够把 mock 的 promise 链（PATCH → 重拉列表 → onChanged）跑完。
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
};

const byText = (text: string) =>
  Array.from(container.querySelectorAll<HTMLButtonElement>("button")).find(
    (node) => node.textContent?.trim() === text,
  );

const field = <T extends HTMLElement>(label: string): T => {
  const node = container.querySelector<T>(`[aria-label="${label}"]`);
  if (!node) throw new Error(`找不到字段：${label}`);
  return node;
};

/**
 * 受控输入要用**原生原型上的 value setter** 赋值再派发事件：直接 `node.value = x`
 * 会撞上 React 自己的 value tracker（React 认为"没变化"，`onChange` 不触发）。
 */
const setField = async (label: string, value: string) => {
  const node = field<
    HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement
  >(label);
  const proto =
    node instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : node instanceof HTMLSelectElement
        ? HTMLSelectElement.prototype
        : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
  if (!setter) throw new Error(`找不到 ${label} 的 value setter`);
  await act(async () => {
    setter.call(node, value);
    node.dispatchEvent(new Event("input", { bubbles: true }));
    node.dispatchEvent(new Event("change", { bubbles: true }));
  });
};

/** 拨动复选框：React 的 checkbox `onChange` 由原生 `click` 驱动（jsdom 会先翻转 `checked`）。 */
const toggleCheckbox = async (label: string) => {
  const node = field<HTMLInputElement>(label);
  await act(async () => node.click());
};

const renderPanel = async (
  servers: McpServer[] = [makeServer()],
  onChanged: () => void | Promise<void> = () => undefined,
) => {
  api = installApiSpies(servers);
  await act(async () => {
    root.render(<McpContent onChanged={onChanged} />);
  });
  await flush();
};

const openEditor = async () => {
  const button = byText("编辑");
  if (!button) throw new Error("找不到「编辑」按钮");
  await act(async () => button.click());
  await flush();
};

const clickSave = async () => {
  const button = byText("保存修改");
  if (!button) throw new Error("找不到「保存修改」按钮");
  await act(async () => button.click());
  await flush();
};

/** 最后一次 PATCH 的 `[serverId, body]`；body 就是**实际会序列化出去的请求体**。 */
const lastPatch = (): [string, Record<string, unknown>] => {
  const calls = api.patch.mock.calls;
  expect(calls.length).toBeGreaterThan(0);
  return calls[calls.length - 1] as unknown as [string, Record<string, unknown>];
};

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  vi.restoreAllMocks();
  act(() => root.unmount());
  container.remove();
});

describe("MCP 最近调用（M2，既有覆盖）", () => {
  it("读取并展示调用耗时与状态", async () => {
    await renderPanel();
    expect(container.textContent).toContain("mcp__demo__echo");
    await act(async () => byText("最近调用")!.click());
    await flush();

    expect(container.textContent).toContain("12 ms");
    expect(container.textContent).toContain("error");
    expect(container.querySelectorAll(".mcp-call-list li").length).toBe(2);
  });
});

describe("F1 编辑已有服务器：打开与预填", () => {
  it("预填能读到的字段；env/headers 是空的，并且'不修改 / 整体替换'用户看得见", async () => {
    // ⚠️ 这份 fixture 刻意**偏离**默认值（enabled=false / 90 秒 / 另一个 cwd）：
    // 沿用默认 fixture（enabled=true、60 秒）时，把这两处写死成 `true` / `"60"`
    // 也照样全绿——实测过（M14/M16 在本文件的早期版本上 0 条变红）。预填断言必须让
    // 「照抄默认值」与「真读服务器」两者可分。
    await renderPanel([
      makeServer({ enabled: false, toolCallTimeoutSeconds: 90, cwd: "/srv/demo" }),
    ]);
    await openEditor();

    expect(field<HTMLInputElement>("编辑服务器名称").value).toBe("demo");
    expect(field<HTMLSelectElement>("编辑传输类型").value).toBe("stdio");
    expect(field<HTMLInputElement>("编辑启动命令").value).toBe("python");
    expect(field<HTMLTextAreaElement>("编辑命令参数").value).toBe("-m\ndemo");
    expect(field<HTMLInputElement>("编辑工作目录").value).toBe("/srv/demo");
    expect(field<HTMLInputElement>("编辑工具超时秒数").value).toBe("90");
    expect(field<HTMLInputElement>("编辑启用状态").checked).toBe(false);

    // 密钥读不到 ⇒ 必须是**空**（空 = 不修改；填 `{}` 或 `""` 都会静默清空凭据）。
    expect(field<HTMLTextAreaElement>("编辑环境变量 JSON").value).toBe("");
    // 提示必须是**用户看得到的文案**，不是只写在代码注释里。
    expect(container.textContent).toContain("重新填写会替换全部键");
    expect(container.textContent).toContain("不会与已保存的键合并");
  });
});

describe("F1 编辑已有服务器：请求体（只发脏字段）", () => {
  it("留空不发密钥：只改名字 ⇒ body 的键恰好只有 name", async () => {
    await renderPanel();
    await openEditor();
    await setField("编辑服务器名称", "renamed");
    await clickSave();

    expect(api.patch).toHaveBeenCalledTimes(1);
    const [serverId, body] = lastPatch();
    expect(serverId).toBe("srv_1");
    // ⚠️ 用**键集合**断言，不用 `toHaveBeenCalledWith`：后者对
    // `{ name, env: undefined }` 是通过的（undefined 属性被忽略）——那正是本任务
    // 最容易写成的空转形态。这里连 `env: {}` / `env: undefined` 都拦得住。
    expect(Object.keys(body)).toEqual(["name"]);
    expect(body).toEqual({ name: "renamed" });
    // 没碰 args 就不该重写 args（否则每次改名都把 args 往返一遍）。
    expect(body).not.toHaveProperty("args");
    expect(body).not.toHaveProperty("env");
    expect(body).not.toHaveProperty("headers");
  });

  it("填了就整体替换：body 里的 env 就是用户输入的那一份", async () => {
    await renderPanel();
    await openEditor();
    await setField("编辑环境变量 JSON", '{"A":"1","B":"2"}');
    await clickSave();

    const [, body] = lastPatch();
    expect(Object.keys(body)).toEqual(["env"]);
    expect(body.env).toEqual({ A: "1", B: "2" });
  });

  it("I1 填了就整体替换：http 服务器填 headers ⇒ body 的键恰好只有 headers", async () => {
    // `headers` 是**凭据字段**（与 `env` 同型：write-only + 整体替换）。在这条用例之前，
    // 全文里 `headers` 只出现在 fixture 注释、`not.toHaveProperty` 和一个空值断言里
    // ——**从来没有一条用例「填了 headers 并保存」**，把发送那一行删掉是 0 条变红。
    await renderPanel([HTTP_SERVER]);
    await openEditor();
    await setField("编辑 HTTP headers JSON", '{"Authorization":"Bearer T"}');
    await clickSave();

    const [, body] = lastPatch();
    expect(Object.keys(body)).toEqual(["headers"]);
    expect(body.headers).toEqual({ Authorization: "Bearer T" });
  });

  it("I1 不碰 headers：http 服务器只改 name ⇒ body 里没有 headers 键", async () => {
    // 与上一条配对：http 表单里 headers textarea **在屏上**（stdio 那份连字段都没有，
    // 所以旧用例的 `not.toHaveProperty("headers")` 是空转的）——空 = 不修改，
    // 一旦这里发出 `{}` 或 `undefined`，用户点一次保存就静默清空凭据。
    await renderPanel([HTTP_SERVER]);
    await openEditor();
    expect(field<HTMLTextAreaElement>("编辑 HTTP headers JSON").value).toBe("");
    await setField("编辑服务器名称", "renamed");
    await clickSave();

    const [, body] = lastPatch();
    expect(Object.keys(body)).toEqual(["name"]);
    expect(body).not.toHaveProperty("headers");
  });

  it("I5（把风险做成可见）：只填一个新 key ⇒ 提交的 env 只含该 key，老 key 会丢", async () => {
    // `env` 是 write-only + **整体替换**（`api/routes/mcp.py:94` + `:104-118` 整行
    // UPDATE）。用户"再加一个 key"时只填新 key，后端拿到的是**这一份**，
    // 已保存的键不会合并进来。所以这里断言的是**丢**，而不是"合并"——
    // 如果哪天后端改成合并，这条会红，提醒把文案一起改掉。
    await renderPanel();
    await openEditor();
    await setField("编辑环境变量 JSON", '{"ONLY_NEW":"1"}');
    await clickSave();

    const [, body] = lastPatch();
    expect(Object.keys(body)).toEqual(["env"]);
    expect(Object.keys(body.env as object)).toEqual(["ONLY_NEW"]);
    expect(body.env).toEqual({ ONLY_NEW: "1" });
  });

  it("传输切到 http：只发 transport + url，**不**发明空值去清另一支", async () => {
    await renderPanel();
    await openEditor();
    await setField("编辑传输类型", "http");
    // 切到 http 后 headers 同样读不到 ⇒ 仍是空 = 不修改。
    expect(field<HTMLTextAreaElement>("编辑 HTTP headers JSON").value).toBe("");
    await setField("编辑 HTTP URL", "https://example.com/mcp");
    await clickSave();

    const [, body] = lastPatch();
    // 恰好两个键：既没把另一支的 command/args 发出去，也**没有**发空串/空数组来"清空"。
    // 归零是**仓储**的职责：`_validated_values`
    // （`storage/sqlite_mcp_server_repository.py:176` 起，归零在 `:196-200`：
    // http ⇒ `command=""` / `args=()`）——前端照后端既有行为走即可。
    expect(Object.keys(body).sort()).toEqual(["transport", "url"]);
    expect(body).toEqual({
      transport: "http",
      url: "https://example.com/mcp",
    });
    expect(Object.values(body)).not.toContain("");
  });

  it("args 往返（I4）：每行一个参数、带空格的参数原样发回，没改就一个字节都不发", async () => {
    await renderPanel([makeServer({ args: ["--config", "a b.json"] })]);
    await openEditor();

    // 回填必须是「换行分隔」：按空格拼回会把 `--config` / `a b.json` 的边界弄丢。
    expect(field<HTMLTextAreaElement>("编辑命令参数").value).toBe(
      "--config\na b.json",
    );

    // 只改名字 ⇒ args 不参与（见上一条的键集合断言）。
    await setField("编辑服务器名称", "renamed");
    await clickSave();
    expect(Object.keys(lastPatch()[1])).toEqual(["name"]);

    // 真改了 args ⇒ 按数组原样发回，**带空格的那一项不被拆开**。
    api.patch.mockClear();
    await openEditor();
    await setField("编辑命令参数", "--config\nc d.json");
    await clickSave();
    const [, body] = lastPatch();
    expect(Object.keys(body)).toEqual(["args"]);
    expect(body.args).toEqual(["--config", "c d.json"]);
  });

  it("I2 多字段脏检查：name+command+cwd+超时+enabled 一次改齐 ⇒ 恰好这 5 个键、值等于输入", async () => {
    // 为什么并成一条：`command`/`cwd`/`toolCallTimeoutSeconds`/`enabled` 这 4 条通道
    // 各删一条都是 **0 条变红**——10 个可写字段的发送侧只覆盖了 5 个。`enabled` 尤其要命：
    // 编辑表单里那个用户看得见的复选框「启用这个服务器」拨动后到底发不发，此前没有任何用例。
    // ⚠️ 输入值**刻意偏离** `emptyEditForm` 的默认值（command ""/cwd ""/超时 "60"/enabled true）
    // 以及 fixture 的值，否则「真读表单」与「照抄默认值」不可分（M14/M16 的教训）。
    await renderPanel([makeServer()]);
    await openEditor();
    await setField("编辑服务器名称", "renamed");
    await setField("编辑启动命令", "node");
    await setField("编辑工作目录", "/srv/demo2");
    await setField("编辑工具超时秒数", "77");
    await toggleCheckbox("编辑启用状态");
    await clickSave();

    expect(api.patch).toHaveBeenCalledTimes(1);
    const [serverId, body] = lastPatch();
    expect(serverId).toBe("srv_1");
    // 键集合**恰好**这 5 个：多一个（比如把 args/url 也塞进来）也红。
    expect(Object.keys(body).sort()).toEqual([
      "command",
      "cwd",
      "enabled",
      "name",
      "toolCallTimeoutSeconds",
    ]);
    expect(body).toEqual({
      name: "renamed",
      command: "node",
      cwd: "/srv/demo2",
      toolCallTimeoutSeconds: 77,
      enabled: false,
    });
  });

  it("没有任何改动时保存：零请求，只给一句提示", async () => {
    await renderPanel();
    await openEditor();
    await clickSave();

    expect(api.patch).not.toHaveBeenCalled();
    expect(container.textContent).toContain("没有需要保存的修改。");
  });
});

describe("F1 编辑已有服务器：校验失败零请求", () => {
  it("缺 name：可见文案 + 不发请求", async () => {
    await renderPanel();
    await openEditor();
    await setField("编辑服务器名称", "   ");
    await clickSave();

    expect(api.patch).not.toHaveBeenCalled();
    expect(container.textContent).toContain("请填写服务器名称。");
    // 表单还开着（用户能继续改）。
    expect(container.querySelector('[aria-label="编辑服务器名称"]')).not.toBeNull();
  });

  it("stdio 缺 command：可见文案 + 不发请求", async () => {
    await renderPanel();
    await openEditor();
    await setField("编辑启动命令", "");
    await clickSave();

    expect(api.patch).not.toHaveBeenCalled();
    expect(container.textContent).toContain("请填写启动命令。");
  });

  it("http 缺 url：可见文案 + 不发请求", async () => {
    await renderPanel([
      makeServer({ transport: "http", command: "", url: "https://old/mcp" }),
    ]);
    await openEditor();
    await setField("编辑 HTTP URL", "");
    await clickSave();

    expect(api.patch).not.toHaveBeenCalled();
    expect(container.textContent).toContain("请填写 HTTP 地址。");
  });
});

describe("F1 编辑已有服务器：失败逐类指名 + 保留输入", () => {
  const phrases = {
    network: "连不上本地服务",
    notFound: "已经不存在了",
    conflict: "已经被别的服务器占用",
    serverError: "本地服务出错了（HTTP 500）",
  } as const;

  /** 断言：**本类**的文案在、其它三类的文案都不在（塌成一句通用话会全红）。 */
  const expectNamed = (expected: keyof typeof phrases) => {
    const text = container.textContent ?? "";
    expect(text).toContain(phrases[expected]);
    for (const [kind, phrase] of Object.entries(phrases)) {
      if (kind === expected) continue;
      expect(text).not.toContain(phrase);
    }
    // 保留用户已填内容：表单没关、输入还在。
    expect(field<HTMLInputElement>("编辑服务器名称").value).toBe("renamed");
  };

  const openAndRename = async () => {
    await renderPanel();
    await openEditor();
    await setField("编辑服务器名称", "renamed");
  };

  it("网络不通", async () => {
    await openAndRename();
    api.patch.mockRejectedValue(new TypeError("Failed to fetch"));
    await clickSave();
    expect(api.patch).toHaveBeenCalledTimes(1);
    expectNamed("network");
  });

  it("404（这条服务器已经没了）", async () => {
    await openAndRename();
    api.patch.mockRejectedValue(
      new ApiClientError(new Response(null, { status: 404 }), {
        error: { message: "MCP server not found: srv_1" },
      }),
    );
    await clickSave();
    expectNamed("notFound");
  });

  it("409（改名撞名）", async () => {
    await openAndRename();
    api.patch.mockRejectedValue(
      new ApiClientError(new Response(null, { status: 409 }), {
        error: { message: "An MCP server named 'renamed' already exists." },
      }),
    );
    await clickSave();
    expectNamed("conflict");
  });

  it("500（带状态码与后端 message，不塌成通用话）", async () => {
    await openAndRename();
    api.patch.mockRejectedValue(
      new ApiClientError(new Response(null, { status: 500 }), {
        error: { message: "Internal error" },
      }),
    );
    await clickSave();
    expectNamed("serverError");
    expect(container.textContent).toContain("Internal error");
  });
});

describe("F1 编辑已有服务器：成功后收尾", () => {
  it("重拉列表 + 可见成功提示 + 关掉表单", async () => {
    const onChanged = vi.fn();
    await renderPanel([makeServer()], onChanged);
    const listCallsBefore = api.list.mock.calls.length;

    await openEditor();
    await setField("编辑服务器名称", "renamed");
    await clickSave();

    // 真实重拉，而不是只弹一句提示。
    expect(api.list.mock.calls.length).toBe(listCallsBefore + 1);
    expect(onChanged).toHaveBeenCalledTimes(1);
    expect(container.textContent).toContain("已保存对「renamed」的修改。");
    expect(
      container.querySelector('[aria-label="编辑服务器名称"]'),
    ).toBeNull();
  });
});

describe("F1 添加表单 / 编辑表单互斥（M2）", () => {
  it("点「编辑」会关掉添加表单：两个表单不同时在屏上", async () => {
    await renderPanel();
    await act(async () => byText("添加服务器")!.click());
    await flush();
    // 添加表单在屏上（它的名称字段就叫「服务器名称」）。
    expect(container.querySelector('[aria-label="服务器名称"]')).not.toBeNull();

    await openEditor();
    // 互斥：添加表单必须已经被关掉，否则两个「保存」同时可点、`aria-label` 定位也歧义。
    expect(container.querySelector('[aria-label="服务器名称"]')).toBeNull();
    expect(
      container.querySelector('[aria-label="编辑服务器名称"]'),
    ).not.toBeNull();
  });

  it("点「添加服务器」会关掉编辑表单", async () => {
    await renderPanel();
    await openEditor();
    expect(
      container.querySelector('[aria-label="编辑服务器名称"]'),
    ).not.toBeNull();

    await act(async () => byText("添加服务器")!.click());
    await flush();
    expect(
      container.querySelector('[aria-label="编辑服务器名称"]'),
    ).toBeNull();
    expect(container.querySelector('[aria-label="服务器名称"]')).not.toBeNull();
  });
});

describe("F1 编辑提示不滞留（M3）", () => {
  it("取消编辑会清掉编辑区留下的提示，不让它滞留在面板顶部", async () => {
    await renderPanel();
    await openEditor();
    // 「没有任何改动」这条提示是**表单还开着**时出现的（成功提示则在关闭后出现），
    // 所以它是唯一能走到「取消编辑」时还挂着的提示。
    await clickSave();
    expect(container.textContent).toContain("没有需要保存的修改。");

    await act(async () => byText("取消编辑")!.click());
    await flush();
    expect(container.textContent).not.toContain("没有需要保存的修改。");
  });

  it("切到添加表单会清掉上一次的成功提示", async () => {
    await renderPanel();
    await openEditor();
    await setField("编辑服务器名称", "renamed");
    await clickSave();
    expect(container.textContent).toContain("已保存对「renamed」的修改。");

    await act(async () => byText("添加服务器")!.click());
    await flush();
    expect(container.textContent).not.toContain("已保存对「renamed」的修改。");
  });
});

/* ------------------------------------------------------------------ F2：工具白名单 + 未值守边界 */

/** 三条工具：两条允许、一条被勾掉（`allowed: false`，但仍出现在清单里）。 */
const SCOPED_SERVER = makeServer({
  toolCount: 3,
  allowedTools: ["echo", "sum"],
  allowUnattended: true,
  tools: [
    {
      publicName: "mcp__demo__echo",
      rawName: "echo",
      description: "Echo",
      effect: "read_only",
      requiresExplicitConfirmation: false,
      allowed: true,
    },
    {
      publicName: "mcp__demo__sum",
      rawName: "sum",
      description: "Sum",
      effect: "read_only",
      requiresExplicitConfirmation: false,
      allowed: true,
    },
    {
      publicName: "mcp__demo__remove",
      rawName: "remove",
      description: "Remove",
      effect: "external_action",
      requiresExplicitConfirmation: true,
      allowed: false,
    },
  ],
});

const openToolPicker = async () => {
  const button = byText("工具");
  if (!button) throw new Error("找不到「工具」按钮");
  await act(async () => button.click());
  await flush();
};

const clickSaveTools = async () => {
  const button = byText("保存工具范围");
  if (!button) throw new Error("找不到「保存工具范围」按钮");
  await act(async () => button.click());
  await flush();
};

describe("F2 工具清单展示（M4：已允许 N / 共 M）", () => {
  it("未配置白名单时显示「已允许 M / 共 M」，并标明默认仅交互可用", async () => {
    await renderPanel([makeServer()]);
    expect(container.textContent).toContain("已允许 1 / 共 1");
    expect(container.textContent).toContain("仅交互可用");
  });

  it("白名单收窄后显示两个数（勾掉一半不谎报全量）", async () => {
    await renderPanel([SCOPED_SERVER]);
    expect(container.textContent).toContain("已允许 2 / 共 3");
    expect(container.textContent).toContain("定时/子代理可用");
  });

  it("被勾掉的工具仍出现在工具清单里（否则用户无法放回来）", async () => {
    await renderPanel([SCOPED_SERVER]);
    await openToolPicker();
    expect(container.textContent).toContain("mcp__demo__remove");
    expect(field<HTMLInputElement>("允许工具 mcp__demo__remove").checked).toBe(
      false,
    );
    expect(field<HTMLInputElement>("允许工具 mcp__demo__echo").checked).toBe(true);
  });
});

describe("F2/M4 协议词只在诊断区（修复波）", () => {
  it("勾选行的主标签是产品名（无 mcp__ 前缀、不挂 rawName 的 title），协议名收进折叠", async () => {
    await renderPanel([SCOPED_SERVER]);
    await openToolPicker();

    const rows = Array.from(
      container.querySelectorAll(".mcp-tool-picker-list li"),
    );
    expect(rows.length).toBe(3);
    for (const row of rows) {
      const label = row.querySelector("strong");
      if (!label) throw new Error("勾选行里找不到标签");
      // 主面板不再出现协议前缀，也不再用 `title` 挂原始名（旧写法会两条都红）。
      expect(label.hasAttribute("title")).toBe(false);
      expect(label.textContent ?? "").not.toContain("mcp__");
    }
    expect(
      rows.map((row) => row.querySelector("strong")?.textContent),
    ).toEqual(["echo", "sum", "remove"]);

    // 协议名 + 原始名**没有丢**，只是收进了诊断折叠（3 行各一个）。
    const diagnostics = Array.from(
      container.querySelectorAll(".mcp-tool-protocol"),
    );
    expect(diagnostics.length).toBe(3);
    expect(diagnostics[0].textContent).toContain("mcp__demo__echo");
    expect(diagnostics[0].textContent).toContain("echo");
    expect(diagnostics[2].textContent).toContain("mcp__demo__remove");
  });
});

describe("F2 保存工具范围：请求体是三态之一", () => {
  it("全选 ⇒ allowedTools 显式发 null（全部允许，不是「缺字段」）", async () => {
    await renderPanel([SCOPED_SERVER]);
    await openToolPicker();
    await toggleCheckbox("允许工具 mcp__demo__remove");
    await clickSaveTools();

    const [serverId, body] = lastPatch();
    expect(serverId).toBe("srv_1");
    expect(Object.keys(body).sort()).toEqual(["allowUnattended", "allowedTools"]);
    expect(body.allowedTools).toBeNull();
  });

  it("全部勾掉 ⇒ allowedTools 是空数组（全部禁用，与 null 不同义）", async () => {
    await renderPanel([SCOPED_SERVER]);
    await openToolPicker();
    await toggleCheckbox("允许工具 mcp__demo__echo");
    await toggleCheckbox("允许工具 mcp__demo__sum");
    await clickSaveTools();

    const [, body] = lastPatch();
    expect(body.allowedTools).toEqual([]);
    expect(body.allowUnattended).toBe(true);
  });

  it("勾掉一条 ⇒ 落库用 rawName（不是公共名）", async () => {
    await renderPanel([SCOPED_SERVER]);
    await openToolPicker();
    await toggleCheckbox("允许工具 mcp__demo__sum");
    await clickSaveTools();

    const [, body] = lastPatch();
    expect(body.allowedTools).toEqual(["echo"]);
  });

  it("允许未值守调用是显式开关：默认关，勾上后 body 里为 true", async () => {
    await renderPanel([makeServer()]);
    await openToolPicker();
    expect(field<HTMLInputElement>("允许未值守调用").checked).toBe(false);
    expect(container.textContent).toContain("只在你自己聊天时可用");

    await toggleCheckbox("允许未值守调用");
    await clickSaveTools();

    const [, body] = lastPatch();
    expect(body.allowUnattended).toBe(true);
    expect(body.allowedTools).toBeNull();
  });

  it("没有任何改动时保存：零请求，只给一句提示", async () => {
    await renderPanel([SCOPED_SERVER]);
    await openToolPicker();
    await clickSaveTools();
    expect(api.patch).not.toHaveBeenCalled();
    expect(container.textContent).toContain("没有需要保存的修改。");
  });
});

describe("F2 reload 失败可见（§6 处置 (a)）", () => {
  it("后端回报 reloadError ⇒ 行内如实显示「设置已保存，但工具没有重新加载」", async () => {
    const failed = makeServer({
      reloadError: "连接 MCP 服务器 demo 超时。",
    });
    await renderPanel([failed]);
    // 加载时就把"两半不一致"摆出来（allowed 标记来自新配置、工具列表来自旧连接）。
    expect(container.textContent).toContain("工具没有重新加载");
    expect(container.textContent).toContain("连接 MCP 服务器 demo 超时。");
  });
});

// ---------------------------------------------------------------------------
// G-6（F1 复审 M-B）：两个**用户可见控件**的真 HTTP 接线覆盖洞
//
// 复审原文：「本文件里以下两条**完全没有任何用例**」——
//   ① 行内「禁用/启用」（`toggleEnabled`）
//   ② 添加服务器表单的「保存并连接」（`createServer`）
// 这两条既不是 F1 新增、也不是 F1 触碰，属**既有遗留**；定性是**覆盖洞**、
// 不是缺陷（两者 `catch` 里都有 `setActionError(...)` 的可见报错）。
//
// 姿势刻意比本文件既有用例更**外一层**：既有用例 `vi.spyOn(chatApi, …)`，钉的是
// "组件传给 `chatApi` 的实参"；这里**只 stub 全局 `fetch`**（真 `chatApi`、真组件），
// 断言的是**线上真正发出去的 `{url, method, body}`**——把
//   按钮 → `toggleEnabled`/`createServer` → `chatApi.*` → `api.ts` 的路径/方法/请求体
// 整条链一次钉住（批次 D 的 `apiWiring.test.ts` 同法）。
//
// F-3（G-6 复审 M-3）：用例 ② 的 transport 是**表单默认值** `"stdio"`，单独看它
// 对"写死 `stdio`"的变异仍然绿。补用例 ③（transport 切到非默认的 `"http"` + 填 URL）
// ⇒ `transport` 的两个取值都被断言，"写死任一个值"都可辨。
// ---------------------------------------------------------------------------

/** 与 `api.ts` 里 `configuredBaseUrl` 同一条推导（不含末尾 `/`）。 */
const MCP_BASE = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");

const mcpPath = (rawUrl: string) =>
  MCP_BASE && rawUrl.startsWith(MCP_BASE)
    ? rawUrl.slice(MCP_BASE.length)
    : rawUrl;

type McpHttpRequest = { url: string; method: string; body: unknown };

describe("G-6：行内「禁用」与添加表单「保存并连接」的真 HTTP 接线", () => {
  let requests: McpHttpRequest[];
  /** "服务端"的一份活在内存里的状态：PATCH 真的改到它，刷新才看得见变化。 */
  let live: McpServer[];

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** 只 stub 全局 `fetch`；未路由的 URL 一律 404（用到即暴露）。 */
  const installFetch = () => {
    requests = [];
    vi.stubGlobal(
      "fetch",
      (input: unknown, init?: RequestInit): Promise<Response> => {
        const raw = String(input);
        const path = mcpPath(raw);
        const routePath = path.split("?")[0];
        const method = (init?.method ?? "GET").toUpperCase();
        const body =
          typeof init?.body === "string" ? JSON.parse(init.body) : init?.body;
        requests.push({ url: path, method, body });
        const respond = (payload: unknown, status = 200) =>
          Promise.resolve(
            new Response(JSON.stringify(payload), {
              status,
              headers: { "Content-Type": "application/json" },
            }),
          );

        if (method === "GET" && routePath === "/mcp/servers")
          return respond({ items: live });
        if (method === "POST" && routePath === "/mcp/servers") {
          const created = makeServer({
            ...(body as Partial<McpServer>),
            id: "srv_created",
            createdAt: "2026-09-15T00:00:00.000Z",
            updatedAt: "2026-09-15T00:00:00.000Z",
          });
          live = [...live, created];
          return respond({ server: created }, 201);
        }
        const patchMatch = /^\/mcp\/servers\/([^/]+)$/.exec(routePath);
        if (method === "PATCH" && patchMatch) {
          live = live.map((server) =>
            server.id === patchMatch[1]
              ? { ...server, ...(body as Partial<McpServer>) }
              : server,
          );
          return respond({
            server: live.find((server) => server.id === patchMatch[1]),
          });
        }
        return respond(
          {
            error: {
              code: "not_found",
              message: `测试未路由：${method} ${routePath}`,
            },
          },
          404,
        );
      },
    );
  };

  const renderFetchPanel = async (
    onChanged: () => void | Promise<void> = () => undefined,
  ) => {
    await act(async () => {
      root.render(<McpContent onChanged={onChanged} />);
    });
    await flush();
  };

  const mcpRoute = (request: McpHttpRequest) => mcpPath(request.url).split("?")[0];
  const listGets = () =>
    requests.filter(
      (request) => request.method === "GET" && mcpRoute(request) === "/mcp/servers",
    );

  it("①点「禁用」（该服务器 enabled: true）⇒ PATCH /mcp/servers/{id}、body enabled === false，且成功后列表确实刷新", async () => {
    live = [makeServer({ enabled: true })];
    installFetch();
    await renderFetchPanel();

    // 前提：行内按钮当前是「禁用」（服务器 enabled），且初次加载只拉了一次列表。
    expect(byText("禁用")).not.toBeUndefined();
    expect(byText("启用")).toBeUndefined();
    expect(listGets()).toHaveLength(1);

    await act(async () => byText("禁用")!.click());
    await flush();

    // 决定性断言：**实际发出**的 PATCH —— 路径带真实 id、方法 PATCH、请求体是**翻转后**的值。
    const patches = requests.filter(
      (request) => request.method === "PATCH",
    );
    expect(patches).toHaveLength(1);
    expect(mcpPath(patches[0].url)).toBe("/mcp/servers/srv_1");
    expect(patches[0].body).toEqual({ enabled: false });
    // 请求体只能有这一个键：多带别的字段（例如把整台服务器发过去）会在这里红。
    expect(Object.keys(patches[0].body as object)).toEqual(["enabled"]);

    // 成功后**列表确实刷新**：`toggleEnabled` 里 `await load()` ⇒ 又发了一次 GET。
    expect(listGets()).toHaveLength(2);

    // UI 结果：刷新回来的那台已禁用 ⇒ 行内按钮翻面成「启用」。
    expect(byText("启用")).not.toBeUndefined();
    expect(byText("禁用")).toBeUndefined();
  });

  it("②填好添加表单点「保存并连接」⇒ POST /mcp/servers，body 关键字段与表单一致", async () => {
    live = [makeServer({ id: "srv_existing", name: "existing" })];
    installFetch();
    await renderFetchPanel();

    await act(async () => byText("添加服务器")!.click());
    await flush();

    await setField("服务器名称", "weather");
    await setField("启动命令", "npx");
    await setField("命令参数", "-y weather-mcp --port 3000");
    await setField("工具超时秒数", "45");

    const save = byText("保存并连接");
    expect(save).not.toBeUndefined();
    await act(async () => save!.click());
    await flush();

    // 决定性断言：**实际发出**的 POST 与表单逐字段对得上（不是"被调用过"）。
    //
    // ⚠️ F-3（G-6 复审 M-3）如实登记的边界：本用例的表单 transport **就是默认值**
    // `"stdio"`（`McpManagement.tsx:29`），所以这一行里的 `transport: "stdio"` **单独**
    // 不能区分"透传表单"与"写死默认值"（把 `transport: form.transport` 改成字面
    // `"stdio"` 时它照样绿——实测过）。它在本文件里仍然**不是永远绿**：它的对照是
    // 用例 ③（表单切到**非默认**的 `"http"`）——写死 `"stdio"` 会在 ③ 红，写死
    // `"http"` 会在**这一行**红。两个值都断言，"写死任一个默认/非默认值"都可辨。
    const posts = requests.filter((request) => request.method === "POST");
    expect(posts).toHaveLength(1);
    expect(mcpPath(posts[0].url)).toBe("/mcp/servers");
    expect(posts[0].body).toMatchObject({
      name: "weather",
      transport: "stdio",
      command: "npx",
      args: ["-y", "weather-mcp", "--port", "3000"],
      toolCallTimeoutSeconds: 45,
    });

    // UI 结果：保存成功后表单关闭、列表刷新（既有那台 + 新建的这台）。
    expect(byText("添加服务器")).not.toBeUndefined();
    expect(byText("保存并连接")).toBeUndefined();
    expect(container.textContent).toContain("weather");
  });

  /**
   * F-3（G-6 复审 M-3）：用例 ② 的表单 transport 用的是**默认值** `"stdio"`，
   * 所以"透传表单"与"写死 `stdio`"在那一行上**不可分**（复审实测：把
   * `transport: form.transport` 改成字面 `"stdio"` ⇒ 仍然绿；对照
   * `toolCallTimeoutSeconds` 写死 ⇒ 红）。
   *
   * 这一条把 transport 切到**非默认**的另一个值（`"http"`，并填 URL 走另一条分支）：
   * 请求体里必须就是它 ⇒ "写死 `stdio`"这一种坏法在这里红。两条用例合起来把
   * transport 的**两个取值都钉住**，"写死任一个值"都可辨 ⇒ 不再有永远绿的断言。
   */
  it("③传输类型切到非默认的 `http` ⇒ POST body.transport 就是它（不是写死的 stdio）", async () => {
    live = [makeServer({ id: "srv_existing", name: "existing" })];
    installFetch();
    await renderFetchPanel();

    await act(async () => byText("添加服务器")!.click());
    await flush();

    await setField("服务器名称", "weather-http");
    // 表单默认 `stdio`（`McpManagement.tsx:29`）；这里刻意切到**另一个**值。
    await setField("传输类型", "http");
    await setField("HTTP URL", "https://weather.example/mcp");

    const save = byText("保存并连接");
    expect(save).not.toBeUndefined();
    // 前置检查：切到 http 后必须**填了 URL**，按钮才是可点的（否则这一点击是空气）。
    expect((save as HTMLButtonElement).disabled).toBe(false);
    await act(async () => save!.click());
    await flush();

    const posts = requests.filter((request) => request.method === "POST");
    expect(posts).toHaveLength(1);
    expect(mcpPath(posts[0].url)).toBe("/mcp/servers");
    // 决定性断言：**非默认**的 transport（与只在 http 分支渲染的 url）真的透传了出去。
    expect(posts[0].body).toMatchObject({
      name: "weather-http",
      transport: "http",
      url: "https://weather.example/mcp",
    });
    // 反向：**不是**默认的 stdio —— 把 `transport: form.transport` 写死成 `"stdio"`
    // （表单默认值）时，上面那一行必红。
    expect((posts[0].body as { transport: string }).transport).not.toBe("stdio");

    // UI 结果：保存成功后表单关闭、列表刷新（既有那台 + 新建的这台）。
    expect(byText("添加服务器")).not.toBeUndefined();
    expect(byText("保存并连接")).toBeUndefined();
    expect(container.textContent).toContain("weather-http");
  });
});
