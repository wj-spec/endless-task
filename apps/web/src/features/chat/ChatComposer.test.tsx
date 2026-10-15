// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { chatApi } from "./api";
import type {
  Conversation,
  ConversationSnapshot,
  RuntimeV2ConversationReference,
  SkillInvocationCandidate,
} from "./apiTypes";
import { ChatComposer } from "./ChatComposer";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const candidates: SkillInvocationCandidate[] = [
  {
    name: "review-notes",
    description: "评审笔记",
    scope: "user",
    source: "~/.agents/skills",
    pinned: true,
  },
  { name: "release-check", description: "发布检查", scope: "user" },
  { name: "other-skill", description: "别的", scope: "workspace" },
];

let container: HTMLDivElement;
let root: Root;
let draft = "";

type ComposerOverrides = {
  conversation: ConversationSnapshot | null;
  mentionAnchorLaneId: string | null;
  sideMode: "temporary_conversation" | "branch_lane" | null;
  variant: "main" | "side";
};

const renderComposer = (
  onSend: (
    references: RuntimeV2ConversationReference[],
  ) => void | Promise<boolean> = () => undefined,
  skills: SkillInvocationCandidate[] = candidates,
  overrides: Partial<ComposerOverrides> = {},
) => {
  const element = () => (
    <ChatComposer
      conversation={overrides.conversation ?? null}
      draft={draft}
      health={null}
      isGenerating={false}
      loading={false}
      mentionAnchorLaneId={overrides.mentionAnchorLaneId}
      otherLaneRunning={false}
      pendingAction={null}
      providers={[]}
      runningLaneLabel=""
      sideMode={overrides.sideMode ?? null}
      skillCandidates={skills}
      steerable={false}
      variant={overrides.variant ?? "main"}
      onCancel={() => undefined}
      onDraftChange={(value) => {
        draft = value;
        act(() => root.render(element()));
      }}
      onModelChange={() => undefined}
      onRemoveFile={() => undefined}
      onSend={onSend}
      onUploadFile={() => undefined}
    />
  );
  act(() => root.render(element()));
};

/** 让候选列表 / 预览那一串 microtask 结算完。 */
const flush = () => act(async () => {
  await Promise.resolve();
  await Promise.resolve();
});

const textarea = () =>
  container.querySelector<HTMLTextAreaElement>("textarea")!;

const type = (value: string) => {
  const node = textarea();
  // React 会拦截直接赋值：必须走原型 setter 才能触发 onChange。
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  )?.set;
  setter?.call(node, value);
  node.setSelectionRange(value.length, value.length);
  act(() => {
    node.dispatchEvent(new Event("input", { bubbles: true }));
  });
};

// S14b：候选浮层 portal 到 body（`.composer` 是 overflow: hidden，留在里面会被裁掉），
// 所以断言一律查 document 而不是测试容器。
const menu = () =>
  document.querySelector<HTMLUListElement>("ul.composer-slash-menu");

const options = () =>
  Array.from(document.querySelectorAll(".composer-slash-menu button")).map(
    (node) => node.querySelector(".composer-slash-name")?.textContent,
  );

const press = (key: string) =>
  act(() => {
    textarea().dispatchEvent(
      new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true }),
    );
  });

beforeEach(() => {
  draft = "";
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.restoreAllMocks();
});

describe("ChatComposer `/技能名` 候选（S1）", () => {
  it("行首输入 / 前缀触发候选并按前缀过滤", () => {
    renderComposer();
    type("/");
    expect(menu()).not.toBeNull();
    expect(options()).toEqual(["/review-notes", "/release-check", "/other-skill"]);

    type("/re");
    expect(options()).toEqual(["/review-notes", "/release-check"]);

    type("/review");
    expect(options()).toEqual(["/review-notes"]);
  });

  it("路径与普通文本不触发", () => {
    renderComposer();
    type("看看 /Users/example 目录");
    expect(menu()).toBeNull();
    type("3/4 的比例");
    expect(menu()).toBeNull();
  });

  it("Enter 接受候选并补空格，不发送消息", () => {
    let sent = 0;
    renderComposer(() => {
      sent += 1;
    });
    type("/re");
    expect(options()).toEqual(["/review-notes", "/release-check"]);
    press("ArrowDown");
    press("Enter");
    expect(draft).toBe("/release-check ");
    expect(sent).toBe(0);
  });

  it("固定进目录的候选带已固定标记", () => {
    renderComposer();
    type("/review");
    expect(document.querySelector(".composer-slash-pin")?.textContent).toBe(
      "已固定",
    );
  });

  it("候选项展示来源标签", () => {
    renderComposer();
    type("/review");
    expect(document.querySelector(".composer-slash-source")?.textContent).toBe(
      "~/.agents/skills",
    );
  });

  it("候选浮层 portal 到 body，不受 .composer 的 overflow 裁剪", () => {
    renderComposer(() => undefined);
    type("/");
    const menuNode = menu();
    expect(menuNode).not.toBeNull();
    // 关键回归：菜单不能留在 .composer 里（它的 overflow: hidden 会把菜单整块裁掉，
    // 用户只看得见"阴影"看不见列表）。
    expect(document.querySelector(".composer .composer-slash-menu")).toBeNull();
    expect(menuNode!.closest(".composer")).toBeNull();
    const anchor = menuNode!.closest(".composer-slash-menu-anchor");
    expect(anchor).not.toBeNull();
    expect(anchor!.parentElement).toBe(document.body);
    // fixed 定位来自 .composer-slash-menu-anchor（jsdom 不加载样式表），
    // 真实可见性由 e2e `slash-menu.spec.ts` 在浏览器里断言。
    expect(anchor!.className).toContain("composer-slash-menu-anchor");
  });

  it("没有候选时给出明确提示（不是没反应）", () => {
    renderComposer(() => undefined, []);
    type("/");
    const empty = document.querySelector(".composer-slash-menu.is-empty");
    expect(empty).not.toBeNull();
    expect(empty!.textContent).toContain("还没有可调用的技能");
    expect(empty!.textContent).toContain("~/.claude/skills");
    expect(menu()).toBeNull();
  });

  it("Escape 关闭候选，Enter 恢复发送", () => {
    let sent = 0;
    renderComposer(() => {
      sent += 1;
    });
    type("/re");
    expect(menu()).not.toBeNull();
    press("Escape");
    expect(menu()).toBeNull();
    press("Enter");
    expect(sent).toBe(1);
  });

  it("候选超过 8 个时全部渲染，不再被截断（回归）", () => {
    const many: SkillInvocationCandidate[] = Array.from(
      { length: 12 },
      (_, index) => ({
        name: "skill-" + String(index).padStart(2, "0"),
        description: "第 " + index + " 个候选",
        scope: "user" as const,
      }),
    );
    renderComposer(() => undefined, many);
    type("/");
    expect(options()).toHaveLength(12);
    expect(options().at(-1)).toBe("/skill-11");
    expect(
      document.querySelector(".composer-slash-more")?.textContent,
    ).toContain("共 12 个技能");
  });

  it("候选不超过一屏时不显示统计行", () => {
    renderComposer();
    type("/");
    expect(options()).toHaveLength(3);
    expect(document.querySelector(".composer-slash-more")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// C3b：`@` 引用会话上下文（候选 / chip / 预览）
// ---------------------------------------------------------------------------

const SENDING: Conversation = {
  id: "conv-c3b-self",
  title: "正在发送的会话",
  status: "active",
  nextTurnOrdinal: 3,
  titleIsManual: false,
  createdAt: "2026-09-01T00:00:00Z",
  updatedAt: "2026-09-02T00:00:00Z",
  archivedAt: null,
  parentConversationId: null,
  forkTurnId: null,
  kind: "normal",
  providerProfileId: null,
  modelOverride: null,
  promotedAt: null,
  workspaceId: "ws-c3b",
};

const OTHER: Conversation = { ...SENDING, id: "conv-c3b-other", title: "另一个会话" };
const ARCHIVED: Conversation = {
  ...SENDING,
  id: "conv-c3b-archived",
  title: "已归档的会话",
  status: "archived",
};

/** G-2：把引用选到 3 条（超过 `DEFAULT_MENTION_REFERENCES = 2`）需要第 3 个可引用会话。 */
const THIRD: Conversation = { ...SENDING, id: "conv-c3b-third", title: "第三个会话" };
const FOURTH: Conversation = {
  ...SENDING,
  id: "conv-c3b-fourth",
  title: "第四个会话",
};

const sendingSnapshot: ConversationSnapshot = {
  conversation: SENDING,
  turns: [],
  files: [],
};

/** 会话切换（I-2）用：同一个 composer 实例、另一个会话。 */
const SECOND: Conversation = {
  ...SENDING,
  id: "conv-c3b-second",
  title: "切换过去的会话",
};
const secondSnapshot: ConversationSnapshot = {
  conversation: SECOND,
  turns: [],
  files: [],
};

const hint = () =>
  document.querySelector<HTMLParagraphElement>(".composer-hint")?.textContent;

/** 主区提示行的**逐字**文案（本批唯一的功能可发现性，见 I-1）。 */
const MAIN_HINT = "Enter 发送 · Shift + Enter 换行 · @ 引用会话上下文 · 可附加 UTF-8 文本（≤ 1 MB）";
/** 侧区文案必须与 `83f8498` 逐字一致：多折一行会把侧栏 `.composer` 底边顶高 17px。 */
const SIDE_HINT = "Enter 发送 · Shift + Enter 换行 · 可附加 UTF-8 文本（≤ 1 MB）";

const mentionMenu = () =>
  document.querySelector<HTMLUListElement>("ul.composer-mention-menu");

const mentionOptions = () =>
  Array.from(document.querySelectorAll(".composer-mention-name")).map(
    (node) => node.textContent,
  );

const chipTitles = () =>
  Array.from(document.querySelectorAll(".composer-mention-title")).map(
    (node) => node.textContent,
  );

const clickIn = (selector: string, predicate?: (node: Element) => boolean) =>
  act(() => {
    const nodes = Array.from(document.querySelectorAll(selector));
    const node = predicate ? nodes.find(predicate) : nodes[0];
    if (!node) throw new Error(`找不到可点击的节点：${selector}`);
    (node as HTMLElement).click();
  });

/** G-2：`@` 候选那一串 microtask（列表 fetch → 渲染）结算完——2 个会抖，给足余量。 */
const settle = (times = 10) =>
  act(async () => {
    for (let i = 0; i < times; i += 1) await Promise.resolve();
  });

/**
 * G-2：敲 `@` → 选中标题匹配的那条候选 → chip 落定（一步引用）。
 *
 * 刻意**自带断言**：候选没渲染出来或点击没生效都直接抛/红，而不是静默少一条
 * chip 让后面的用例变成"断空气"。
 */
const pickMention = async (title: string) => {
  const before = chipTitles().length;
  type("@");
  await settle();
  // jsdom 专属坑：上一步 `acceptMention` 排的 `requestAnimationFrame`
  // （`node.focus()` + `setSelectionRange(replaced.length)`）落在本次 `type` **之后**，
  // 会把光标拉回 0；而 `acceptMention` 靠"光标前的文本"找 `@` 查询，光标在 0 时
  // 直接 `if (!match) return;` ⇒ 点击被静默吞掉。点击是**同步**读 `selectionStart`，
  // 所以紧贴点击前把光标放回 `@` 之后即可（真实浏览器里 rAF 早于人手指的下一击）。
  textarea().setSelectionRange(1, 1);
  const option = Array.from(
    document.querySelectorAll<HTMLElement>(".composer-mention-menu button"),
  ).find((node) => node.textContent?.includes(title) === true);
  if (!option) {
    throw new Error(
      `候选浮层里找不到「${title}」，实际候选：${mentionOptions().join(" / ")}`,
    );
  }
  act(() => option.click());
  await settle();
  expect(chipTitles()).toHaveLength(before + 1);
};

/** G-2：`DEFAULT_MENTION_REFERENCES` 超限提示的**可见文本**（`role=status` 那条警告）。 */
const mentionOverflowWarning = () =>
  document.querySelector<HTMLElement>(".composer-mention-warn[role='status']")
    ?.textContent ?? null;

/** G-2：整个 composer 的可见文本（措辞/结构变了也逃不掉，只要用户还看得见）。 */
const composerVisibleText = () => container.querySelector(".composer")?.textContent ?? "";

describe("ChatComposer `@` 引用会话上下文（C3b）", () => {
  let listSpy: ReturnType<typeof vi.spyOn>;
  let previewSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    // 真 `chatApi` + `vi.spyOn`：不做 `vi.mock("./api")` 的整模块替换
    //（那是批次 D/F1/F2 反复出现的"改坏了也不红"的盲区）。
    listSpy = vi
      .spyOn(chatApi, "listConversations")
      .mockResolvedValue([SENDING, OTHER, ARCHIVED]);
    previewSpy = vi.spyOn(chatApi, "previewRuntimeV2References").mockResolvedValue({
      conversationId: SENDING.id,
      totalTokenEstimate: 4321,
      warnings: [],
      items: [
        {
          conversationId: OTHER.id,
          title: OTHER.title,
          tokenEstimate: 1234,
          text: "被引用的正文（测试里不关心内容）",
        },
      ],
    });
  });

  it("`@` 打开候选：只列同工作区、未归档的其它会话，且浮层 portal 到 body", async () => {
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    type("@");
    await flush();

    expect(listSpy).toHaveBeenCalledWith("active", undefined, "ws-c3b");
    expect(mentionMenu()).not.toBeNull();
    // 自己不能引用自己（后端 `mention_self_reference` 指名 400）；
    // 归档会话不可引用（后端 404），两条都在候选列表里就被筛掉。
    expect(mentionOptions()).toEqual(["另一个会话"]);
    // 与 `/` 菜单同一套骨架：portal 到 body，否则被 `.composer` 的 overflow 裁掉。
    expect(document.querySelector(".composer .composer-mention-menu")).toBeNull();
    expect(mentionMenu()!.closest(".composer-slash-menu-anchor")?.parentElement).toBe(
      document.body,
    );
  });

  it("邮箱这类中间位置的 @ 不触发候选（也不拉列表）", async () => {
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    // 结尾就是域名：只要丢了"行首或空白后"这条约束就会误触发。
    type("联系 a@b.com");
    await flush();
    expect(mentionMenu()).toBeNull();
    expect(listSpy).not.toHaveBeenCalled();
  });

  it("选中候选 ⇒ 出现 chip，草稿里的 @查询 被抹掉", async () => {
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    type("@另一");
    await flush();
    expect(mentionOptions()).toEqual(["另一个会话"]);
    clickIn(".composer-mention-menu button");
    await flush();
    expect(chipTitles()).toEqual(["另一个会话"]);
    expect(draft).toBe("");
  });

  it("删除 chip ⇒ 引用状态清空", async () => {
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    type("@另一");
    await flush();
    clickIn(".composer-mention-menu button");
    await flush();
    expect(chipTitles()).toEqual(["另一个会话"]);
    clickIn(".composer-mention button");
    await flush();
    expect(chipTitles()).toEqual([]);
    expect(document.querySelector(".composer-mentions")).toBeNull();
  });

  it("选了引用 ⇒ 调预览端点，chip 与合计的 token 数字都来自响应", async () => {
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    type("@另一");
    await flush();
    clickIn(".composer-mention-menu button");
    await flush();

    // 送出去的引用形状（判别联合的一个成员）+ 一审的 `entryIds` 预留字段。
    expect(previewSpy).toHaveBeenCalledWith(SENDING.id, [
      {
        kind: "conversation",
        conversationId: OTHER.id,
        mode: "summary",
        entryIds: [],
      },
    ]);
    // 数字必须来自响应：合计 4321、单条 1234，前端一个都不算。
    expect(
      document.querySelector(".composer-mention-total")?.textContent,
    ).toContain("4321");
    expect(
      document.querySelector(".composer-mention-tokens")?.textContent,
    ).toContain("1234");
  });

  it("切换档位 ⇒ 预览带上 recent + recentTurns（与后端可选值同值）", async () => {
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    type("@另一");
    await flush();
    clickIn(".composer-mention-menu button");
    await flush();
    previewSpy.mockClear();

    const select = document.querySelector<HTMLSelectElement>(
      ".composer-mention-mode",
    )!;
    act(() => {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLSelectElement.prototype,
        "value",
      )?.set;
      setter?.call(select, "recent:5");
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await flush();
    expect(previewSpy).toHaveBeenCalledWith(SENDING.id, [
      {
        kind: "conversation",
        conversationId: OTHER.id,
        mode: "recent",
        recentTurns: 5,
        entryIds: [],
      },
    ]);
  });

  it("预览被后端明确拒绝（4xx）⇒ 显示后端文案，且拦住发送", async () => {
    previewSpy.mockRejectedValue(
      Object.assign(new Error("被引用的会话已归档，不可引用：conv-c3b-other"), {
        status: 404,
      }),
    );
    let sent = 0;
    renderComposer(
      () => {
        sent += 1;
      },
      candidates,
      {
        conversation: sendingSnapshot,
        mentionAnchorLaneId: "lane-c3b",
      },
    );
    type("@另一");
    await flush();
    clickIn(".composer-mention-menu button");
    await flush();

    expect(
      document.querySelector(".composer-mention-error")?.textContent,
    ).toContain("已归档");
    // 后端已经说了这条引用不合法：不能把用户送进必然 400 的提交（提交路径上还有
    // 自动命名等侧写入），所以这一步一个消息请求都不该发。
    type("帮我看看");
    press("Enter");
    expect(sent).toBe(0);
  });

  it("发送：onSend 收到的就是已选引用（形状与协议一致，content 由调用方从草稿取）", async () => {
    const sent: RuntimeV2ConversationReference[][] = [];
    renderComposer(
      (references) => {
        sent.push(references);
      },
      candidates,
      {
        conversation: sendingSnapshot,
        mentionAnchorLaneId: "lane-c3b",
      },
    );
    type("@另一");
    await flush();
    clickIn(".composer-mention-menu button");
    await flush();
    type("把上面的结论也带上");
    press("Enter");

    expect(sent).toHaveLength(1);
    expect(sent[0]).toEqual([
      {
        kind: "conversation",
        conversationId: OTHER.id,
        mode: "summary",
        entryIds: [],
      },
    ]);
  });

  it("没有可挂载 lane 的会话 ⇒ 明确说明、不给候选，且不拉候选也不预览", async () => {
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: null,
    });
    type("@");
    await flush();

    // 后端对这种会话指名 400 `mention_no_lane_to_anchor`：前端直接不让选。
    expect(listSpy).not.toHaveBeenCalled();
    expect(previewSpy).not.toHaveBeenCalled();
    expect(mentionMenu()).toBeNull();
    const empty = document.querySelector(".composer-mention-menu.is-empty");
    expect(empty).not.toBeNull();
    expect(empty!.textContent).toContain("先发一条消息");
  });

  it("提示行：主区逐字保留 `@ 引用会话上下文`，侧区逐字不含（I-1 + 布局契约）", () => {
    // 这行文字是本批**唯一**的功能可发现性：placeholder 里没有 `@`、工具栏没有入口、
    // 浮层只有敲了 `@` 才出现。删掉它 ⇒ 用户只能靠猜（复审 I-1）。
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    expect(hint()).toBe(MAIN_HINT);

    // 侧区那份提示是 `visibility: hidden`，**仍占布局盒**：文案多折一行会把侧栏
    // footer 顶高、`.composer` 底边上移 17px（`branch-temporary.spec.ts:91` 的
    // 主/侧底边对齐契约）。所以侧区必须与基线逐字一致。
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
      variant: "side",
      sideMode: "branch_lane",
    });
    expect(hint()).toBe(SIDE_HINT);
  });

  it("会话切换 ⇒ 清空 chip，且下一条消息不再带旧引用（I-2 跨会话泄漏回归）", async () => {
    const sent: RuntimeV2ConversationReference[][] = [];
    renderComposer(
      (references) => {
        sent.push(references);
        return Promise.resolve(true);
      },
      candidates,
      { conversation: sendingSnapshot, mentionAnchorLaneId: "lane-c3b" },
    );
    type("@另一");
    await flush();
    clickIn(".composer-mention-menu button");
    await flush();
    expect(chipTitles()).toEqual([OTHER.title]);

    // 切到另一个会话：主区 `<ChatWorkSurface>` **不重挂载**（`App.tsx` 里没有 `key`），
    // 所以 composer 的本地 state 会原样留着——必须显式重置。
    renderComposer(
      (references) => {
        sent.push(references);
        return Promise.resolve(true);
      },
      candidates,
      { conversation: secondSnapshot, mentionAnchorLaneId: "lane-c3b" },
    );
    await flush();
    expect(chipTitles()).toEqual([]);
    expect(document.querySelector(".composer-mentions")).toBeNull();

    // 在会话 B 里发一条：不许带上会话 A 选的那条引用。
    type("在 B 里发一条");
    press("Enter");
    await flush();
    expect(sent).toEqual([[]]);
  });

  it("预览返回 warnings ⇒ 在 role=alert 上显示后端告警（I-3：R-C4 的明确告警）", async () => {
    previewSpy.mockResolvedValue({
      conversationId: SENDING.id,
      totalTokenEstimate: 4321,
      warnings: ["投影被截断：单轮引用超出预算，仅注入前 3 条"],
      items: [
        {
          conversationId: OTHER.id,
          title: OTHER.title,
          tokenEstimate: 1234,
          text: "被引用的正文（测试里不关心内容）",
        },
      ],
    });
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    type("@另一");
    await flush();
    clickIn(".composer-mention-menu button");
    await flush();

    // 告警必须真的渲染出来：token 数字来自后端只是 R-C4 的一半，"超阈值明确告警"
    // 是另一半（后端 `plan.warnings`）。此前测试里 `warnings` 恒为 `[]` ⇒ 展示块删了也不红。
    const warn = document.querySelector<HTMLElement>(
      ".composer-mention-warn[role='alert']",
    );
    expect(warn).not.toBeNull();
    expect(warn!.textContent).toContain("投影被截断：单轮引用超出预算，仅注入前 3 条");
  });

  it("发送成功（onSend 解析为 true）⇒ 清空 chip，下一条消息不再重复注入", async () => {
    const sent: RuntimeV2ConversationReference[][] = [];
    renderComposer(
      (references) => {
        sent.push(references);
        return Promise.resolve(true);
      },
      candidates,
      { conversation: sendingSnapshot, mentionAnchorLaneId: "lane-c3b" },
    );
    type("@另一");
    await flush();
    clickIn(".composer-mention-menu button");
    await flush();
    type("把上面的结论也带上");
    press("Enter");
    await flush();

    // §6 裁定：发送成功后清空——不清的话同一条引用会被静默带进后续每一条消息。
    expect(sent).toEqual([
      [
        {
          kind: "conversation",
          conversationId: OTHER.id,
          mode: "summary",
          entryIds: [],
        },
      ],
    ]);
    expect(chipTitles()).toEqual([]);

    type("再发一条");
    press("Enter");
    await flush();
    expect(sent).toEqual([
      [
        {
          kind: "conversation",
          conversationId: OTHER.id,
          mode: "summary",
          entryIds: [],
        },
      ],
      [],
    ]);
  });

  it("发送失败（onSend 解析为 false 或抛错）⇒ 保留 chip，别让用户重填", async () => {
    renderComposer(() => Promise.resolve(false), candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    type("@另一");
    await flush();
    clickIn(".composer-mention-menu button");
    await flush();
    type("这条会被拒");
    press("Enter");
    await flush();
    expect(chipTitles()).toEqual([OTHER.title]);

    // 抛错同样按"没发出去"处理（发送路径自己会显示错误文案，这里只保证不丢引用）。
    renderComposer(() => Promise.reject(new Error("提交失败")), candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    type("@另一");
    await flush();
    clickIn(".composer-mention-menu button");
    await flush();
    type("这条也会被拒");
    press("Enter");
    await flush();
    expect(chipTitles()).toEqual([OTHER.title]);
  });

  it("候选超过 50 个 ⇒ 只渲染 50 条，并给出总数提示（M-1 防御上限）", async () => {
    const many: Conversation[] = Array.from({ length: 60 }, (_, index) => ({
      ...OTHER,
      id: `conv-c3b-many-${index}`,
      title: `批量会话 ${String(index).padStart(2, "0")}`,
    }));
    listSpy.mockResolvedValue([SENDING, ...many]);
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    type("@");
    await flush();

    // `menuWindow` 的 50 条上限是**防御性上限**（菜单可滚动）：候选再多也不能把
    // 浮层渲成 60 行的巨物（也防 DOM 爆炸）。
    expect(mentionOptions()).toHaveLength(50);
    expect(mentionMenu()!.querySelectorAll("li")).toHaveLength(50);
    expect(
      document.querySelector(".composer-mention-more")?.textContent,
    ).toContain("共 60 个会话");
  });

  // -------------------------------------------------------------------------
  // G-2（C3b 复审 M-6）：本地"看得见的提示"0 守护
  // -------------------------------------------------------------------------
  it("引用选到 3 条 ⇒ composer **可见文本**里出现「已选 3 条，超过默认上限 2 条」", async () => {
    listSpy.mockResolvedValue([SENDING, OTHER, THIRD, FOURTH]);
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    await pickMention(OTHER.title);
    await pickMention(THIRD.title);
    await pickMention(FOURTH.title);

    // 前提：真的有 3 条 chip（否则下面断的是空气）。
    expect(chipTitles()).toEqual([OTHER.title, THIRD.title, FOURTH.title]);

    // 断言落在**可见文本**上，不是 API 载荷的 `warnings`（那条早有 `apiWiring.test.ts`
    // 守着，正是它掩盖了这句提示本身零守护）。删掉 `ChatComposer.tsx` 里那句渲染 ⇒ 红。
    expect(composerVisibleText()).toContain("已选 3 条，超过默认上限 2 条");
    expect(mentionOverflowWarning()).toContain("已选 3 条，超过默认上限 2 条");
  });

  it("边界：恰好 2 条（等于默认上限）⇒ **不出现**该提示", async () => {
    listSpy.mockResolvedValue([SENDING, OTHER, THIRD, FOURTH]);
    renderComposer(() => undefined, candidates, {
      conversation: sendingSnapshot,
      mentionAnchorLaneId: "lane-c3b",
    });
    await pickMention(OTHER.title);
    await pickMention(THIRD.title);

    expect(chipTitles()).toEqual([OTHER.title, THIRD.title]);
    // `>` 而不是 `>=`：第 3 条起才提示（判据来自 `DEFAULT_MENTION_REFERENCES = 2`）。
    expect(mentionOverflowWarning()).toBeNull();
    expect(composerVisibleText()).not.toContain("超过默认上限");
  });
});
