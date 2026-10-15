import { useCallback, useEffect, useState } from "react";
import { FormErrorSummary } from "../ui/FormErrorSummary";
import { ConfirmDialog } from "../ui/ConfirmDialog";
import { EmptyState } from "../ui/EmptyState";
import { ApiClientError, chatApi } from "../chat/api";
import type {
  McpCallRecord,
  McpServer,
  McpServerInput,
} from "../chat/apiTypes";

type McpContentProps = {
  onChanged?: () => void | Promise<void>;
};

type McpForm = {
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string;
  url: string;
  env: string;
  headers: string;
  toolCallTimeoutSeconds: string;
};

const emptyForm: McpForm = {
  name: "",
  transport: "stdio",
  command: "",
  args: "",
  url: "",
  env: "{}",
  headers: "{}",
  toolCallTimeoutSeconds: "60",
};

function parseMapping(value: string): Record<string, string> {
  if (!value.trim()) return {};
  const parsed: unknown = JSON.parse(value);
  if (
    typeof parsed !== "object" ||
    parsed === null ||
    Array.isArray(parsed) ||
    Object.entries(parsed).some(([, item]) => typeof item !== "string")
  ) {
    throw new Error("JSON 必须是字符串到字符串的对象");
  }
  return parsed as Record<string, string>;
}

/* ------------------------------------------------------------------ F1：编辑已有服务器 */

/**
 * 编辑表单的值。字段集合与添加表单一致，**两处刻意的差别**都是契约逼出来的：
 *
 * 1. `args` 是「**每行一个参数**」（添加表单是空格分隔）。库里存的是 `tuple[str, ...]`
 *    （`storage/sqlite_mcp_server_repository.py:193`），而编辑态**拿不到**当初的切分
 *    ⇒ 按空格回填会把 `--config` / `a b.json` 这种参数**拆坏**。每行一个参数是
 *    唯一能原样往返的表示。
 * 2. `env` / `headers` 用**空串**表示「不修改」。`GET /mcp/servers` 的 `_mcp_json`
 *    （`api/routes/mcp.py:23-51`）**刻意不回显**这两个字段 ⇒ 表单**拿不到**已存密钥；
 *    而 PATCH 对它们是**整体替换**（`api/routes/mcp.py:94`/`:97` + `:104-118` 整行 UPDATE）
 *    ⇒ 空值**绝不能**被发送，否则用户点一次保存就把凭据静默清空。
 */
type McpEditForm = {
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string;
  cwd: string;
  url: string;
  env: string;
  headers: string;
  toolCallTimeoutSeconds: string;
  enabled: boolean;
};

const emptyEditForm: McpEditForm = {
  name: "",
  transport: "stdio",
  command: "",
  args: "",
  cwd: "",
  url: "",
  env: "",
  headers: "",
  toolCallTimeoutSeconds: "60",
  enabled: true,
};

/** 密钥字段的可见提示（I5）：必须让用户看到「整体替换、不合并」，而不是只写在注释里。 */
const SECRET_FIELD_HINT =
  "密钥留空表示不修改（沿用已保存的值）；重新填写会替换全部键，不会与已保存的键合并。";

/** `args` 输入的规则说明（I4）。 */
const ARGS_LINE_PLACEHOLDER = "每行一个参数（带空格的参数写成一行，不会被拆开）";

/** 把一条服务器**能读到的**字段回填成编辑表单（`env`/`headers` 读不到 ⇒ 空 = 不修改）。 */
function editFormOf(server: McpServer): McpEditForm {
  return {
    name: server.name,
    transport: server.transport,
    command: server.command,
    args: server.args.join("\n"),
    cwd: server.cwd,
    url: server.url,
    env: "",
    headers: "",
    toolCallTimeoutSeconds: String(server.toolCallTimeoutSeconds),
    enabled: server.enabled,
  };
}

function parseArgsLines(value: string): string[] {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

/**
 * 保存失败的**逐类指名**（与 `ScheduledTasks.tsx` 的 `taskActionErrorText` 同口径：
 * 认不出来的原因**原样**带上状态码与后端 message，既不吞也不塌成一句通用话）。
 *
 * 这条路的真实失败面（后端 `PATCH /mcp/servers/{id}`）：
 * - 404 `NotFoundError`：这条服务器已经不存在了（`sqlite_mcp_server_repository.py:134`）；
 * - 409 `ConflictError`：改名撞上另一条服务器的 `name`（`:139-142`）；
 * - 4xx `ValidationError`：字段校验（`:176-209`）；
 * - 5xx / 其它：带状态码与后端 message；
 * - 网络不通**不是** `ApiClientError`，单独一句。
 */
const mcpEditErrorText = (error: unknown): string => {
  if (error instanceof ApiClientError) {
    if (error.status === 404) {
      return "这条 MCP 服务器已经不存在了（可能已在别处删除），修改没有保存。";
    }
    if (error.status === 409) {
      return "现在不能保存：这个名称已经被别的服务器占用了，换一个名字再试。";
    }
    if (error.status >= 500) {
      return `本地服务出错了（HTTP ${error.status}），修改没有保存：${error.message}`;
    }
    return `修改没有保存（HTTP ${error.status}）：${error.message}`;
  }
  return "修改没有保存：连不上本地服务，请确认 API 已启动。";
};

/**
 * 编辑表单的必填口径与添加表单**逐字一致**（`name` 必填；stdio 必填 `command`；
 * http 必填 `url`），另外把「解析不了 JSON」和超时范围也摆出来。
 *
 * 与添加表单的**表现**不同：这里**不靠禁用按钮**表达校验失败——禁用只让点击无效，
 * 用户看不到原因；所以保存按钮始终可点，问题在这里逐条列出（并且**零请求**）。
 */
const editValidationErrors = (form: McpEditForm): string[] => {
  const problems: string[] = [];
  if (!form.name.trim()) problems.push("请填写服务器名称。");
  if (form.transport === "stdio" && !form.command.trim()) {
    problems.push("请填写启动命令。");
  }
  if (form.transport === "http" && !form.url.trim()) {
    problems.push("请填写 HTTP 地址。");
  }
  if (form.env.trim()) {
    try {
      parseMapping(form.env);
    } catch (error) {
      problems.push(
        `环境变量 JSON 不正确：${error instanceof Error ? error.message : "无法解析"}`,
      );
    }
  }
  if (form.headers.trim()) {
    try {
      parseMapping(form.headers);
    } catch (error) {
      problems.push(
        `HTTP headers JSON 不正确：${error instanceof Error ? error.message : "无法解析"}`,
      );
    }
  }
  const timeout = Number(form.toolCallTimeoutSeconds);
  if (!Number.isFinite(timeout) || timeout <= 0 || timeout > 600) {
    problems.push("工具超时秒数要填 1 到 600 之间的数字。");
  }
  return problems;
};

/**
 * 只把**用户真的改过**的字段放进 PATCH body（独立发现 #3）。
 *
 * 为什么必须逐字段比对而不是整份提交：PATCH 是「缺字段 = 保留；给了该字段 = 整体替换」
 * （`api/routes/mcp.py:90-103`）。整份提交等于每次改名都把 `env`/`headers` 也重写一遍，
 * 而这两个字段**读不回来** ⇒ 那是一次静默的凭据清空。
 *
 * `env`/`headers` 的判定**不是**「与原值比较」（拿不到原值），而是「用户有没有填」：
 * 空 = 不修改（连 `{}` 都不发），填了 = 整体替换该字段。
 */
const buildEditPatch = (
  form: McpEditForm,
  original: McpEditForm,
): Partial<McpServerInput> => {
  const patch: Partial<McpServerInput> = {};
  if (form.name.trim() !== original.name) patch.name = form.name.trim();
  if (form.transport !== original.transport) patch.transport = form.transport;
  if (form.command.trim() !== original.command) {
    patch.command = form.command.trim();
  }
  if (form.args !== original.args) patch.args = parseArgsLines(form.args);
  if (form.cwd.trim() !== original.cwd) patch.cwd = form.cwd.trim();
  if (form.url.trim() !== original.url) patch.url = form.url.trim();
  if (form.env.trim()) patch.env = parseMapping(form.env);
  if (form.headers.trim()) patch.headers = parseMapping(form.headers);
  if (form.toolCallTimeoutSeconds !== original.toolCallTimeoutSeconds) {
    patch.toolCallTimeoutSeconds = Number(form.toolCallTimeoutSeconds);
  }
  if (form.enabled !== original.enabled) patch.enabled = form.enabled;
  // ⚠️ 传输切换**不需要**在这里显式清空另一支的字段：仓储 `_validated_values`
  // （`storage/sqlite_mcp_server_repository.py:176` 起，归零在 `:196-200`）已按 transport
  // 归零（stdio ⇒ `url=""`/`headers={}`；http ⇒ `command=""`/`args=()`）。
  // 往这里补 `command: ""` / `args: []` 只会与后端既有行为矛盾（且 `null` 语义是
  // 「保留」，前端根本无法用空值清空 `command`/`cwd`/`url`）。
  return patch;
};

/* ------------------------------------------------------------------ F2：工具白名单 + 未值守边界 */

/**
 * F2 的原理说明（ADR §5 的用户可见安全承诺，逐条对上）：
 *
 * - 「新加的服务器默认只在你自己聊天时可用」——列里默认 `allowUnattended=false`
 *   （迁移 076 的列默认 0；**只有迁移前就存在的服务器**被回填 1，行为不变）。
 * - 「写类工具每次调用都会弹确认」——不是新加的：没有 `readOnlyHint` 的 MCP 工具今天
 *   就是 `approval_mode=REQUIRED`（`mcp_runtime/tool.py`），这里只是把它说出来，
 *   避免用户以为"打开了未值守就能无人值守地写"。
 * - 协议词（`stdio`/`http`/endpoint/`mcp__` 前缀/原始工具名）只在**高级设置/诊断**里
 *   出现（`product-visible-semantics-plan.md:108/202/308`）。
 *   M4（修复波）：勾选行以前把原始名挂在 `<strong title>`（主面板上的悬停诊断）、
 *   并直接渲染带 `mcp__` 前缀的 `publicName`——现在主标签用 `toolProductName`
 *   （服务器前缀剥掉的产品名），完整协议名 + 原始名收进行内「协议名（诊断用）」折叠。
 */
const UNATTENDED_HINT =
  "默认只在你自己聊天时可用；写类工具每次调用都会弹确认。要让定时任务/子代理也能用它，需要在这里显式打开。";

const TOOL_SCOPE_HINT =
  "没勾的工具不会进助手工具列表；勾掉后仍会留在这个清单里，随时可以再勾回来。";

/** 白名单里没有配置（= 全部允许）时的展示名。 */
const ALL_TOOLS_LABEL = "全部工具";

function isAllAllowed(server: McpServer): boolean {
  return server.allowedTools === null;
}

/** 「已允许 N / 共 M」（M4：两个数都来自后端，不许只显示 M）。 */
function toolScopeLabel(server: McpServer): string {
  const total = server.tools.length;
  if (isAllAllowed(server)) {
    return `已允许 ${total} / 共 ${total}`;
  }
  const allowed = server.tools.filter((tool) => tool.allowed).length;
  return `已允许 ${allowed} / 共 ${total}`;
}

/** 打开工具勾选面板时的本地候选集（`undefined` = 按当前配置）。 */
function pickFor(server: McpServer): Record<string, boolean> {
  const pick: Record<string, boolean> = {};
  for (const tool of server.tools) {
    pick[tool.publicName] = tool.allowed;
  }
  return pick;
}

/**
 * 由勾选状态算出 PATCH body 的 `allowedTools`（三态，后端 I6 裁定）：
 * `null` = 未配置 = 全部允许；`[]` = 全部禁用；非空 = 只允许这些 `rawName`。
 *
 * ⚠️ `null` 与"缺字段"是**两件事**：PATCH 的"缺字段"= 保留现值（`routes/mcp.py`），
 * 所以"全选 ⇒ 回到全部允许"必须**显式发 `null`**，不能靠省略字段。
 */
function allowedToolsPatch(
  server: McpServer,
  pick: Record<string, boolean>,
): string[] | null {
  if (server.tools.every((tool) => pick[tool.publicName] !== false)) {
    return null;
  }
  return server.tools
    .filter((tool) => pick[tool.publicName] !== false)
    .map((tool) => tool.rawName);
}

/** 勾选状态与服务器当前白名单是否等价（决定"保存"是否需要发请求 / 是否提示无改动）。 */
function pickMatchesServer(
  server: McpServer,
  pick: Record<string, boolean>,
  unattended: boolean,
): boolean {
  const allowed = server.tools
    .filter((tool) => pick[tool.publicName] !== false)
    .map((tool) => tool.rawName);
  if (allowed.length === server.tools.length) {
    if (server.allowedTools !== null) return false;
  } else {
    const current = [...(server.allowedTools ?? [])].sort();
    if (current.length !== allowed.length) return false;
    if (current.some((name, index) => name !== [...allowed].sort()[index])) {
      return false;
    }
  }
  return unattended === server.allowUnattended;
}

/**
 * `reloadError`（§6 处置 (a)）：保存成功但重新加载工具失败时，**如实**告诉用户
 * "这条设置已经存下来了，但工具列表还没重新加载"——不能让用户以为已经生效。
 */
function reloadNoticeOf(server: McpServer): string | null {
  if (!server.reloadError) return null;
  return `设置已保存，但重新加载工具失败：${server.reloadError}`;
}

/**
 * M4（修复波）：勾选行的**产品名**——去掉 `mcp__<服务器>__` 这个协议前缀。
 *
 * 口径：主面板只出现产品名；完整**协议名**（`publicName`）与**原始工具名**
 * （`rawName`）都收进下面行内的「协议名（诊断用）」折叠里
 * （`product-visible-semantics-plan.md:108/308`：MCP 协议名只在高级设置或诊断中出现）。
 * 前缀对不上（服务器改名、桥的名字是旧的）时原样返回，不猜。
 */
function toolProductName(serverName: string, publicName: string): string {
  const prefix = `mcp__${serverName}__`;
  return publicName.startsWith(prefix)
    ? publicName.slice(prefix.length)
    : publicName;
}

export function McpContent({ onChanged }: McpContentProps) {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [form, setForm] = useState<McpForm>(emptyForm);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmRemove, setConfirmRemove] = useState<McpServer | null>(null);
  const [calls, setCalls] = useState<Record<string, McpCallRecord[]>>({});
  const [callsBusyId, setCallsBusyId] = useState<string | null>(null);
  /** 正在编辑哪一条（独立发现 #3：编辑需要**新状态**，不复用「是否在添加」那个开关）。 */
  const [editing, setEditing] = useState<{
    id: string;
    original: McpEditForm;
  } | null>(null);
  const [editForm, setEditForm] = useState<McpEditForm>(emptyEditForm);
  const [editErrors, setEditErrors] = useState<string[] | null>(null);
  const [editNotice, setEditNotice] = useState<string | null>(null);
  const [editBusy, setEditBusy] = useState(false);
  /** F2：正在给哪一条勾工具（`null` = 工具面板关着）。 */
  const [openTools, setOpenTools] = useState<string | null>(null);
  const [toolPick, setToolPick] = useState<Record<string, boolean>>({});
  const [toolUnattended, setToolUnattended] = useState(false);
  const [toolsBusy, setToolsBusy] = useState(false);

  const loadCalls = useCallback(async (serverId: string) => {
    setCallsBusyId(serverId);
    try {
      const items = await chatApi.listMcpCalls(serverId, 10);
      setCalls((current) => ({ ...current, [serverId]: items }));
    } catch {
      setCalls((current) => ({ ...current, [serverId]: [] }));
    } finally {
      setCallsBusyId(null);
    }
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setServers(await chatApi.listMcpServers());
      setLoadError(null);
    } catch {
      setLoadError("无法加载 MCP 服务器，请重试。");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const updateForm = (patch: Partial<McpForm>) => {
    setForm((current) => ({ ...current, ...patch }));
  };

  const createServer = async () => {
    setActionError(null);
    try {
      const env = parseMapping(form.env);
      const headers = parseMapping(form.headers);
      await chatApi.createMcpServer({
        name: form.name.trim(),
        transport: form.transport,
        command: form.command.trim(),
        args: form.args
          .split(/\s+/)
          .map((item) => item.trim())
          .filter(Boolean),
        env,
        headers,
        url: form.url.trim(),
        toolCallTimeoutSeconds: Number(form.toolCallTimeoutSeconds || "60"),
      });
      setForm(emptyForm);
      setAdding(false);
      await load();
      await onChanged?.();
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : "添加 MCP 服务器失败，请重试。",
      );
    }
  };

  const toggleEnabled = async (server: McpServer) => {
    setBusyId(server.id);
    setActionError(null);
    try {
      await chatApi.patchMcpServer(server.id, { enabled: !server.enabled });
      await load();
      await onChanged?.();
    } catch {
      setActionError("更新 MCP 服务器失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  const reloadServer = async (server: McpServer) => {
    setBusyId(server.id);
    setActionError(null);
    try {
      await chatApi.reloadMcpServer(server.id);
      await load();
      await onChanged?.();
    } catch {
      setActionError("重连 MCP 服务器失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  const removeServer = async (server: McpServer) => {
    setBusyId(server.id);
    setActionError(null);
    try {
      await chatApi.deleteMcpServer(server.id);
      await load();
      await onChanged?.();
    } catch {
      setActionError("删除 MCP 服务器失败，请重试。");
    } finally {
      setBusyId(null);
    }
  };

  const updateEditForm = (patch: Partial<McpEditForm>) => {
    setEditForm((current) => ({ ...current, ...patch }));
  };

  const openEdit = (server: McpServer) => {
    const prefill = editFormOf(server);
    // 添加与编辑互斥：两个表单同时在屏上会让"保存"指向不明。
    setAdding(false);
    // F2：工具面板与编辑表单也互斥（同一行里两个"保存"会让 aria-label 定位歧义）。
    setOpenTools(null);
    setEditing({ id: server.id, original: prefill });
    setEditForm(prefill);
    setEditErrors(null);
    setEditNotice(null);
    setActionError(null);
  };

  /* ---------------------------------------------------------- F2：工具勾选面板 */

  const openToolPicker = (server: McpServer) => {
    setAdding(false);
    setEditing(null);
    setOpenTools(server.id);
    setToolPick(pickFor(server));
    setToolUnattended(server.allowUnattended);
    setEditNotice(null);
    setActionError(null);
  };

  const closeToolPicker = () => {
    setOpenTools(null);
    setEditNotice(null);
  };

  const toggleTool = (publicName: string) => {
    setToolPick((current) => ({
      ...current,
      [publicName]: current[publicName] === false,
    }));
  };

  const saveTools = async (server: McpServer) => {
    if (!pickMatchesServer(server, toolPick, toolUnattended)) {
      setToolsBusy(true);
      setActionError(null);
      setEditNotice(null);
      try {
        const updated = await chatApi.patchMcpServer(server.id, {
          allowedTools: allowedToolsPatch(server, toolPick),
          allowUnattended: toolUnattended,
        });
        setOpenTools(null);
        setEditNotice(
          reloadNoticeOf(updated) ??
            `已保存「${server.name}」的工具范围。`,
        );
        await load();
        await onChanged?.();
      } catch (error) {
        setEditErrors(null);
        setActionError(mcpEditErrorText(error));
      } finally {
        setToolsBusy(false);
      }
      return;
    }
    setEditNotice("没有需要保存的修改。");
  };

  const cancelEdit = () => {
    setEditing(null);
    setEditErrors(null);
    // M-3：提示是**面板级**的（渲染在列表上方），取消编辑时必须一起清掉，
    // 否则「没有需要保存的修改。」会滞留在顶部。
    setEditNotice(null);
  };

  const saveEdit = async () => {
    if (!editing) return;
    setActionError(null);
    setEditNotice(null);
    const problems = editValidationErrors(editForm);
    if (problems.length > 0) {
      // 校验失败 ⇒ **零请求**（不调 PATCH），把问题逐条摆出来。
      setEditErrors(problems);
      return;
    }
    setEditErrors(null);
    const patch = buildEditPatch(editForm, editing.original);
    if (Object.keys(patch).length === 0) {
      setEditNotice("没有需要保存的修改。");
      return;
    }
    setEditBusy(true);
    try {
      await chatApi.patchMcpServer(editing.id, patch);
      const savedName = editForm.name.trim();
      setEditing(null);
      setEditNotice(`已保存对「${savedName}」的修改。`);
      await load();
      await onChanged?.();
    } catch (error) {
      // 失败 ⇒ **保留用户已填内容**（不关表单、不重置 `editForm`），如实指名原因。
      setEditErrors([mcpEditErrorText(error)]);
    } finally {
      setEditBusy(false);
    }
  };

  return (
    <div className="panel-content">
      <div className="knowledge-toolbar">
        <p className="mcp-summary">
          MCP 工具会进入助手工具列表；外部动作仍走审批，危险工具始终确认。
        </p>
        <button
          onClick={() => {
            if (adding) {
              setAdding(false);
              return;
            }
            setAdding(true);
            // 与编辑表单互斥（见 `openEdit`）；同样把面板级的编辑提示清掉（M-3）。
            setEditing(null);
            setOpenTools(null);
            setEditErrors(null);
            setEditNotice(null);
          }}
          type="button"
        >
          {adding ? "取消" : "添加服务器"}
        </button>
      </div>
      {editNotice ? (
        <p className="file-tree-note" role="status">
          {editNotice}
        </p>
      ) : null}
      <FormErrorSummary
        error={actionError}
        heading="服务器操作没有完成"
        className="proposal-error form-error-summary"
      />
      {adding ? (
        <div className="knowledge-form mcp-form">
          <input
            aria-label="服务器名称"
            onChange={(event) => updateForm({ name: event.target.value })}
            placeholder="名称（小写字母、数字、-、_）"
            value={form.name}
          />
          <select
            aria-label="传输类型"
            onChange={(event) =>
              updateForm({
                transport: event.target.value as "stdio" | "http",
              })
            }
            value={form.transport}
          >
            <option value="stdio">stdio</option>
            <option value="http">HTTP</option>
          </select>
          {form.transport === "stdio" ? (
            <>
              <input
                aria-label="启动命令"
                onChange={(event) => updateForm({ command: event.target.value })}
                placeholder="命令，例如 npx"
                value={form.command}
              />
              <input
                aria-label="命令参数"
                onChange={(event) => updateForm({ args: event.target.value })}
                placeholder="参数（空格分隔）"
                value={form.args}
              />
              <textarea
                aria-label="环境变量 JSON"
                onChange={(event) => updateForm({ env: event.target.value })}
                placeholder='{"API_TOKEN":"..."}'
                value={form.env}
              />
            </>
          ) : (
            <>
              <input
                aria-label="HTTP URL"
                onChange={(event) => updateForm({ url: event.target.value })}
                placeholder="https://example.com/mcp"
                value={form.url}
              />
              <textarea
                aria-label="HTTP headers JSON"
                onChange={(event) => updateForm({ headers: event.target.value })}
                placeholder='{"Authorization":"Bearer ..."}'
                value={form.headers}
              />
            </>
          )}
          <input
            aria-label="工具超时秒数"
            inputMode="numeric"
            onChange={(event) =>
              updateForm({ toolCallTimeoutSeconds: event.target.value })
            }
            value={form.toolCallTimeoutSeconds}
          />
          <div className="memory-actions">
            <button
              disabled={!form.name.trim() || (form.transport === "stdio" && !form.command.trim()) || (form.transport === "http" && !form.url.trim())}
              onClick={() => void createServer()}
              type="button"
            >
              保存并连接
            </button>
          </div>
        </div>
      ) : null}
      {loading ? (
        <div aria-hidden="true" className="skeleton-panel">
          <span className="skeleton-line" />
          <span className="skeleton-line is-short" />
          <span className="skeleton-line" />
        </div>
      ) : null}
      {!loading && loadError ? (
        <EmptyState
          action={
            <button onClick={() => void load()} type="button">
              重试
            </button>
          }
          desc={loadError}
          title="没加载出来"
        />
      ) : null}
      {!loading && !loadError && servers.length === 0 ? (
        <EmptyState
          desc="添加一个本地 stdio MCP 服务器或 HTTP MCP 服务，工具会自动进入助手工具列表。"
          title="还没有 MCP 服务器"
        />
      ) : null}
      {servers.map((server) => (
        <div className="memory-item knowledge-item" key={server.id}>
          <p className="memory-content knowledge-title">{server.name}</p>
          <div className="memory-meta">
            <span className="knowledge-badge">{server.transport}</span>
            <span className={`mcp-state is-${server.state}`}>{server.state}</span>
            {/* F2 / M4：白名单落地后不能只显示"共 M 个"——勾掉一半仍显示 40 会与
                勾选状态打架，所以两个数都显示（都来自后端：`tools[].allowed`）。 */}
            <span className="knowledge-badge">{toolScopeLabel(server)}</span>
            <span className="knowledge-badge">
              {server.allowUnattended ? "定时/子代理可用" : "仅交互可用"}
            </span>
          </div>
          <p className="memory-content mcp-command">
            {server.transport === "stdio"
              ? [server.command, ...server.args].filter(Boolean).join(" ")
              : server.url}
          </p>
          {server.lastError ? (
            <FormErrorSummary
              error={server.lastError}
              heading="连接没有完成"
              className="proposal-error form-error-summary"
            />
          ) : null}
          {/* F2（§6 处置 (a)）：保存成功但 reload 失败 ⇒ 同一行里如实说"设置存下来了、
              工具列表没重新加载"，不让用户以为白名单已经生效。 */}
          {reloadNoticeOf(server) ? (
            <FormErrorSummary
              error={reloadNoticeOf(server)}
              heading="设置已保存，但工具没有重新加载"
              className="proposal-error form-error-summary"
            />
          ) : null}
          {server.tools.length > 0 ? (
            <div className="mcp-tools">
              {server.tools.map((tool) => (
                <div className="mcp-tool" key={tool.publicName}>
                  <strong>{tool.publicName}</strong>
                  <span>{tool.description}</span>
                </div>
              ))}
            </div>
          ) : null}
          <div className="memory-actions">
            <button
              disabled={busyId === server.id}
              onClick={() => void toggleEnabled(server)}
              type="button"
            >
              {server.enabled ? "禁用" : "启用"}
            </button>
            <button
              disabled={busyId === server.id}
              onClick={() => void reloadServer(server)}
              type="button"
            >
              重连
            </button>
            <button
              disabled={callsBusyId === server.id}
              onClick={() => void loadCalls(server.id)}
              type="button"
            >
              {callsBusyId === server.id ? "读取中…" : "最近调用"}
            </button>
            {/* F2：工具范围入口。面板里能看到**服务器暴露的全部工具**（含被勾掉的），
                否则用户收窄白名单后就再也放不回来。白名单已配置但当前一个都没允许时
                （工具列表仍由 status().tools 原样给出），也要留入口让用户改回来。 */}
            <button
              disabled={busyId === server.id}
              onClick={() => openToolPicker(server)}
              type="button"
            >
              工具
            </button>
            {/* F1：第 5 个并列按钮（这一行**没有** `RowMenu`，照既有 4 个的样式加）。 */}
            <button
              disabled={busyId === server.id}
              onClick={() => openEdit(server)}
              type="button"
            >
              编辑
            </button>
            <button
              className="danger-action"
              disabled={busyId === server.id}
              onClick={() => setConfirmRemove(server)}
              type="button"
            >
              删除
            </button>
          </div>
          {editing?.id === server.id ? (
            <div className="knowledge-form mcp-form">
              <FormErrorSummary error={editErrors} heading="修改没有保存" />
              <input
                aria-label="编辑服务器名称"
                onChange={(event) =>
                  updateEditForm({ name: event.target.value })
                }
                placeholder="名称（小写字母、数字、-、_）"
                value={editForm.name}
              />
              <select
                aria-label="编辑传输类型"
                onChange={(event) =>
                  updateEditForm({
                    transport: event.target.value as "stdio" | "http",
                  })
                }
                value={editForm.transport}
              >
                <option value="stdio">stdio</option>
                <option value="http">HTTP</option>
              </select>
              {editForm.transport === "stdio" ? (
                <>
                  <input
                    aria-label="编辑启动命令"
                    onChange={(event) =>
                      updateEditForm({ command: event.target.value })
                    }
                    placeholder="命令，例如 npx"
                    value={editForm.command}
                  />
                  <textarea
                    aria-label="编辑命令参数"
                    onChange={(event) =>
                      updateEditForm({ args: event.target.value })
                    }
                    placeholder={ARGS_LINE_PLACEHOLDER}
                    value={editForm.args}
                  />
                  <textarea
                    aria-label="编辑环境变量 JSON"
                    onChange={(event) =>
                      updateEditForm({ env: event.target.value })
                    }
                    placeholder='{"API_TOKEN":"..."}'
                    value={editForm.env}
                  />
                </>
              ) : (
                <>
                  <input
                    aria-label="编辑 HTTP URL"
                    onChange={(event) =>
                      updateEditForm({ url: event.target.value })
                    }
                    placeholder="https://example.com/mcp"
                    value={editForm.url}
                  />
                  <textarea
                    aria-label="编辑 HTTP headers JSON"
                    onChange={(event) =>
                      updateEditForm({ headers: event.target.value })
                    }
                    placeholder='{"Authorization":"Bearer ..."}'
                    value={editForm.headers}
                  />
                </>
              )}
              <input
                aria-label="编辑工作目录"
                onChange={(event) =>
                  updateEditForm({ cwd: event.target.value })
                }
                placeholder="工作目录（可留空）"
                value={editForm.cwd}
              />
              <input
                aria-label="编辑工具超时秒数"
                inputMode="numeric"
                onChange={(event) =>
                  updateEditForm({ toolCallTimeoutSeconds: event.target.value })
                }
                value={editForm.toolCallTimeoutSeconds}
              />
              <label className="file-tree-note">
                <input
                  aria-label="编辑启用状态"
                  checked={editForm.enabled}
                  onChange={(event) =>
                    updateEditForm({ enabled: event.target.checked })
                  }
                  type="checkbox"
                />
                启用这个服务器
              </label>
              <p className="file-tree-note">{SECRET_FIELD_HINT}</p>
              <div className="memory-actions">
                <button
                  disabled={editBusy}
                  onClick={() => void saveEdit()}
                  type="button"
                >
                  {editBusy ? "保存中…" : "保存修改"}
                </button>
                <button disabled={editBusy} onClick={cancelEdit} type="button">
                  取消编辑
                </button>
              </div>
            </div>
          ) : null}
          {openTools === server.id ? (
            <div className="knowledge-form mcp-form mcp-tool-picker">
              <p className="memory-content knowledge-title">
                「{server.name}」的工具范围
              </p>
              <p className="file-tree-note">{TOOL_SCOPE_HINT}</p>
              {server.tools.length === 0 ? (
                <p className="file-tree-note">
                  这个服务器当前没有暴露工具。保存后只在它重新连接（或远端改了工具清单）
                  时生效。
                </p>
              ) : (
                <ul className="mcp-tool-picker-list">
                  {server.tools.map((tool) => (
                    <li key={tool.publicName}>
                      <label>
                        <input
                          aria-label={`允许工具 ${tool.publicName}`}
                          checked={toolPick[tool.publicName] !== false}
                          onChange={() => toggleTool(tool.publicName)}
                          type="checkbox"
                        />
                        <strong>{toolProductName(server.name, tool.publicName)}</strong>
                      </label>
                      <span>{tool.description}</span>
                      {/* M4（修复波）：协议名 + 原始名收进诊断折叠里，不再挂在
                          `<strong title=...>`（那是个主面板上的悬停诊断）。 */}
                      <details className="file-tree-note mcp-tool-protocol">
                        <summary>协议名（诊断用）</summary>
                        <span>
                          {tool.publicName}（原始名：{tool.rawName}）
                        </span>
                      </details>
                      {tool.requiresExplicitConfirmation ? (
                        <span className="file-tree-note">每次调用都会确认</span>
                      ) : null}
                      {toolPick[tool.publicName] === false ? (
                        <span className="file-tree-note">未允许</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
              <label className="file-tree-note">
                <input
                  aria-label="允许未值守调用"
                  checked={toolUnattended}
                  onChange={(event) => setToolUnattended(event.target.checked)}
                  type="checkbox"
                />
                允许定时任务 / 子代理（未值守运行）调用这个服务器的工具
              </label>
              <p className="file-tree-note">{UNATTENDED_HINT}</p>
              <div className="memory-actions">
                <button
                  disabled={toolsBusy}
                  onClick={() => void saveTools(server)}
                  type="button"
                >
                  {toolsBusy ? "保存中…" : "保存工具范围"}
                </button>
                <button disabled={toolsBusy} onClick={closeToolPicker} type="button">
                  取消
                </button>
              </div>
            </div>
          ) : null}
          {calls[server.id] ? (
            calls[server.id].length === 0 ? (
              <p className="file-tree-note">还没有调用记录。</p>
            ) : (
              <ul className="mcp-call-list">
                {calls[server.id].map((item, index) => (
                  <li
                    className={item.status === "ok" ? "is-ok" : "is-failed"}
                    key={`${item.time}-${index}`}
                  >
                    <code>{item.operation}</code>
                    <span className="mcp-call-meta">
                      {item.durationMs} ms · {item.status}
                    </span>
                    <span className="mcp-call-time">
                      {item.time ? new Date(item.time).toLocaleTimeString() : "—"}
                    </span>
                  </li>
                ))}
              </ul>
            )
          ) : null}
        </div>
      ))}
      {confirmRemove ? (
        <ConfirmDialog
          body={`删除 MCP 服务器「${confirmRemove.name}」？该服务器下的工具将从会话中移除。`}
          confirmLabel="删除服务器"
          onClose={() => setConfirmRemove(null)}
          onConfirm={() => {
            const target = confirmRemove;
            setConfirmRemove(null);
            if (target) void removeServer(target);
          }}
          title="删除 MCP 服务器"
          tone="danger"
        />
      ) : null}
    </div>
  );
}
