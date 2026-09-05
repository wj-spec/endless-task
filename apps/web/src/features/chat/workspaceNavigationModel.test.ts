import { describe, expect, it } from "vitest";
import type { Conversation, Workspace } from "./apiTypes";
import {
  buildWorkspaceNavigationModel,
  conversationTitle,
} from "./workspaceNavigationModel";

const workspace = (over: Partial<Workspace>): Workspace => ({
  id: "ws-1",
  name: "工作区一",
  rootPath: "/tmp/ws1",
  createdAt: "2026-09-01T00:00:00Z",
  updatedAt: "2026-09-01T00:00:00Z",
  ...over,
});

const conversation = (over: Partial<Conversation>): Conversation => ({
  id: "conv-1",
  title: "会话",
  status: "active",
  nextTurnOrdinal: 1,
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
  workspaceId: "ws-1",
  ...over,
});

describe("conversationTitle", () => {
  it("空/空白标题回退为「未命名对话」，非空去首尾空白", () => {
    expect(conversationTitle(conversation({ title: "  " }))).toBe("未命名对话");
    expect(conversationTitle(conversation({ title: "  真标题  " }))).toBe("真标题");
  });
});

describe("buildWorkspaceNavigationModel", () => {
  it("按工作区分组且组内会话按更新时间降序", () => {
    const model = buildWorkspaceNavigationModel({
      workspaces: [workspace({})],
      byWorkspace: {
        "ws-1": [
          conversation({ id: "old", updatedAt: "2026-09-01T00:00:00Z" }),
          conversation({ id: "new", updatedAt: "2026-09-05T00:00:00Z" }),
        ],
      },
      general: [],
    });
    expect(model.groups).toHaveLength(1);
    expect(model.groups[0].totalCount).toBe(2);
    expect(model.groups[0].conversations.map((item) => item.id)).toEqual([
      "new",
      "old",
    ]);
  });

  it("工作区无会话时 totalCount=0；缺映射键安全", () => {
    const model = buildWorkspaceNavigationModel({
      workspaces: [workspace({ id: "empty" })],
      byWorkspace: {},
      general: [],
    });
    expect(model.groups[0].conversations).toEqual([]);
    expect(model.groups[0].totalCount).toBe(0);
  });

  it("未归属会话只进 recent（不做伪工作区）并按时间降序", () => {
    const model = buildWorkspaceNavigationModel({
      workspaces: [],
      byWorkspace: {},
      general: [
        conversation({ id: "g-old", workspaceId: null, updatedAt: "2026-09-01T00:00:00Z" }),
        conversation({ id: "g-new", workspaceId: null, updatedAt: "2026-09-06T00:00:00Z" }),
      ],
    });
    expect(model.groups).toHaveLength(0);
    expect(model.recent.map((item) => item.id)).toEqual(["g-new", "g-old"]);
  });
});
