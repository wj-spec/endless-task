import type { KnowledgeProposal,
  CapabilitySnapshot,
  PendingProposal,
  ArtifactDetailSnapshot,
  ArtifactProposal,
  ArtifactRecordSummary,
  ArtifactVersionRecord,
  Conversation,
  ConversationSnapshot,
  ConversationStatus,
  HealthSnapshot,
  ProviderModel,
  ProviderProfile,
  ProviderProfileInput,
  MemoryProposal,
  KnowledgeSource,
  MemoryRecord,
  PermissionSettings,
  RuntimeV2MessageResponse,
  RuntimeV2Metrics,
  RuntimeV2RunUsageCost,
  RuntimeV2MemoryCreateResponse,
  RuntimeV2MemoryListResponse,
  RuntimeV2MemoryPromotionCreateResponse,
  RuntimeV2MemoryPromotionListResponse,
  RuntimeV2MemoryPromotionResolveResponse,
  RuntimeV2MemoryPromotionTarget,
  RuntimeV2RegenerateResponse,
  RuntimeV2LaneCreateInput,
  RuntimeV2LaneCreateResponse,
  RuntimeV2LaneListResponse,
  RuntimeV2LanePromoteResponse,
  RuntimeV2LaneTreeUpdateResponse,
  RuntimeV2LaneUpdateResponse,
  RuntimeV2TemporaryConversationCreateInput,
  RuntimeV2TemporaryConversationCreateResponse,
  RuntimeV2TemporaryConversationPromoteResponse,
  RuntimeV2ProductEvent,
  RuntimeV2RecoveryResponse,
  RuntimeV2RunSelectResponse,
  RuntimeV2RunVariantListResponse,
  RuntimeV2Snapshot,
  GlobalSearchResponse,
  UploadedTextFile,
  BrowseItem,
  EffectLogEntry,
  McpServer,
  McpServerInput,
  Skill,
  Workspace,
  WorkspaceSnapshot,
  SkillPackagesResponse,
  TrajectoryMeta,
  TrajectoryBundleSummary,
  TrajectoryListResponse,
  TaskProposal,
  TaskSummary,
  TaskRun,
  TaskNotification,
  Reminder,
} from "./apiTypes";

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.replace(/\/$/, "") ?? "";

type ApiErrorEnvelope = {
  error?: {
    code?: string;
    message?: string;
    retryable?: boolean;
    correlationId?: string;
  };
};

export class ApiClientError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly correlationId?: string;

  constructor(response: Response, payload?: ApiErrorEnvelope) {
    super(payload?.error?.message ?? `请求失败（${response.status}）`);
    this.name = "ApiClientError";
    this.status = response.status;
    this.code = payload?.error?.code ?? "request_failed";
    this.retryable = payload?.error?.retryable ?? response.status >= 500;
    this.correlationId = payload?.error?.correlationId;
  }
}

const url = (path: string) => `${configuredBaseUrl}${path}`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url(path), {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    let payload: ApiErrorEnvelope | undefined;
    try {
      payload = (await response.json()) as ApiErrorEnvelope;
    } catch {
      payload = undefined;
    }
    throw new ApiClientError(response, payload);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

async function responseError(response: Response): Promise<ApiClientError> {
  let payload: ApiErrorEnvelope | undefined;
  try {
    payload = (await response.json()) as ApiErrorEnvelope;
  } catch {
    payload = undefined;
  }
  return new ApiClientError(response, payload);
}

export const chatApi = {
  health: () => request<HealthSnapshot>("/health"),
  capabilities: () => request<CapabilitySnapshot>("/capabilities"),
  listProviders: async () => {
    const response = await request<{ items: ProviderProfile[] }>("/providers");
    return response.items;
  },
  createProvider: (provider: ProviderProfileInput) =>
    request<{ profile: ProviderProfile }>("/providers", {
      method: "POST",
      body: JSON.stringify(provider),
    }).then((response) => response.profile),
  patchProvider: (providerId: string, patch: Partial<ProviderProfileInput>) =>
    request<{ profile: ProviderProfile }>(`/providers/${providerId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }).then((response) => response.profile),
  deleteProvider: (providerId: string) =>
    request<void>(`/providers/${providerId}`, { method: "DELETE" }),
  setDefaultProvider: (providerId: string) =>
    request<{ profile: ProviderProfile }>("/providers/default", {
      method: "POST",
      body: JSON.stringify({ profileId: providerId }),
    }).then((response) => response.profile),
  refreshProviderModels: (providerId: string) =>
    request<{ profile: ProviderProfile }>(
      `/providers/${providerId}/refresh-models`,
      { method: "POST" },
    ).then((response) => response.profile),
  addProviderModel: (
    providerId: string,
    model: { modelId: string; displayName?: string },
  ) =>
    request<{ profile: ProviderProfile; model: ProviderModel }>(
      `/providers/${providerId}/models`,
      {
        method: "POST",
        body: JSON.stringify(model),
      },
    ),
  patchProviderModel: (
    providerId: string,
    modelId: string,
    patch: { enabled: boolean },
  ) =>
    request<{ model: ProviderModel }>(
      `/providers/${providerId}/models/${encodeURIComponent(modelId)}`,
      {
        method: "PATCH",
        body: JSON.stringify(patch),
      },
    ).then((response) => response.model),
  deleteProviderModel: (providerId: string, modelId: string) =>
    request<void>(
      `/providers/${providerId}/models/${encodeURIComponent(modelId)}`,
      { method: "DELETE" },
    ),
  setProviderDefaultModel: (providerId: string, modelId: string) =>
    request<{ profile: ProviderProfile }>(
      `/providers/${providerId}/default-model`,
      {
        method: "POST",
        body: JSON.stringify({ modelId }),
      },
    ).then((response) => response.profile),
  listConversations: async (
    status: ConversationStatus,
    query?: string,
    workspace?: string | null,
  ) => {
    const parameters = new URLSearchParams({ status });
    if (query?.trim()) parameters.set("query", query.trim());
    if (workspace !== undefined) {
      parameters.set("workspace", workspace ?? "general");
    }
    const response = await request<{ items: Conversation[] }>(
      `/conversations?${parameters.toString()}`,
    );
    return response.items;
  },
  createConversation: (workspaceId: string) =>
    request<Conversation>("/conversations", {
      method: "POST",
      body: JSON.stringify({ workspaceId }),
    }),
  listWorkspaces: async () => {
    const response = await request<{ items: Workspace[] }>("/workspaces");
    return response.items;
  },
  createWorkspace: async (name: string, rootPath?: string | null) => {
    const response = await request<{ workspace: Workspace }>("/workspaces", {
      method: "POST",
      body: JSON.stringify({ name, rootPath: rootPath ?? null }),
    });
    return response.workspace;
  },
  patchWorkspace: (workspaceId: string, rootPath: string | null) =>
    request<{ workspace: Workspace }>(`/workspaces/${workspaceId}`, {
      method: "PATCH",
      body: JSON.stringify({ rootPath }),
    }).then((response) => response.workspace),
  deleteWorkspace: async (workspaceId: string) => {
    await request<void>(`/workspaces/${workspaceId}`, {
      method: "DELETE",
    });
  },
  browseFilesystem: async (path?: string, showHidden = false) => {
    const parameters = new URLSearchParams();
    if (path) parameters.set("path", path);
    if (showHidden) parameters.set("showHidden", "true");
    const response = await request<{ currentPath: string; items: BrowseItem[] }>(
      `/filesystem/browse?${parameters.toString()}`,
    );
    return response;
  },
  listMcpServers: async () => {
    const response = await request<{ items: McpServer[] }>("/mcp/servers");
    return response.items;
  },
  createMcpServer: (server: McpServerInput) =>
    request<{ server: McpServer }>("/mcp/servers", {
      method: "POST",
      body: JSON.stringify(server),
    }).then((response) => response.server),
  patchMcpServer: (serverId: string, patch: Partial<McpServerInput>) =>
    request<{ server: McpServer }>(`/mcp/servers/${serverId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }).then((response) => response.server),
  deleteMcpServer: (serverId: string) =>
    request<void>(`/mcp/servers/${serverId}`, { method: "DELETE" }),
  reloadMcpServer: (serverId: string) =>
    request<{ server: McpServer }>(`/mcp/servers/${serverId}/reload`, {
      method: "POST",
    }).then((response) => response.server),
  listSkills: async (workspaceId?: string | null) => {
    const parameters = new URLSearchParams();
    if (workspaceId) parameters.set("workspace", workspaceId);
    const suffix = parameters.size > 0 ? `?${parameters.toString()}` : "";
    const response = await request<{
      userSkillsDirectory: string;
      items: Skill[];
    }>(`/skills${suffix}`);
    return response;
  },
  patchSkill: (
    scope: "user" | "workspace",
    name: string,
    disabled: boolean,
    workspaceId?: string | null,
  ) => {
    const parameters = new URLSearchParams();
    if (workspaceId) parameters.set("workspace", workspaceId);
    const suffix = parameters.size > 0 ? `?${parameters.toString()}` : "";
    return request<{ disabled: boolean }>(
      `/skills/${scope}/${encodeURIComponent(name)}${suffix}`,
      {
        method: "PATCH",
        body: JSON.stringify({ disabled }),
      },
    );
  },
  shellLog: (workspaceId: string, limit = 100) =>
    request<{ items: EffectLogEntry[] }>(
      `/workspaces/${workspaceId}/shell-log?limit=${limit}`,
    ).then((response) => response.items),
  revealInFinder: (path: string) =>
    request<{ revealed: boolean }>("/filesystem/reveal", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  getConversation: (conversationId: string) =>
    request<ConversationSnapshot>(`/conversations/${conversationId}`),
  patchConversation: (
    conversationId: string,
    patch: {
      title?: string;
      status?: ConversationStatus;
      providerProfileId?: string | null;
      modelOverride?: string | null;
    },
  ) =>
    request<Conversation>(`/conversations/${conversationId}`, {
      method: "PATCH",
      body: JSON.stringify(patch),
    }),
  deleteConversation: (conversationId: string) =>
    request<void>(`/conversations/${conversationId}`, { method: "DELETE" }),
  uploadFile: async (conversationId: string, file: File) => {
    const parameters = new URLSearchParams({ filename: file.name });
    const response = await fetch(
      url(`/conversations/${conversationId}/files?${parameters.toString()}`),
      {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": file.type || "application/octet-stream",
        },
        body: file,
      },
    );
    if (!response.ok) throw await responseError(response);
    return (await response.json()) as UploadedTextFile;
  },
  deleteFile: (conversationId: string, fileId: string) =>
    request<void>(`/conversations/${conversationId}/files/${fileId}`, {
      method: "DELETE",
    }),
  createBranch: (conversationId: string, forkTurnId?: string) =>
    request<{ conversation: Conversation }>(
      `/conversations/${conversationId}/branches`,
      {
        method: "POST",
        body: JSON.stringify(forkTurnId ? { forkTurnId } : {}),
      },
    ),
  promoteConversation: (conversationId: string) =>
    request<{ conversation: Conversation }>(
      `/conversations/${conversationId}/promote`,
      { method: "POST" },
    ),
  getRuntimeV2Snapshot: (conversationId: string, laneId?: string | null) => {
    const params = new URLSearchParams();
    if (laneId) params.set("lane_id", laneId);
    const query = params.toString();
    return request<RuntimeV2Snapshot>(
      `/api/v2/conversations/${conversationId}/snapshot${query ? `?${query}` : ""}`,
    );
  },
  createRuntimeV2Message: (
    conversationId: string,
    content: string,
    laneId?: string | null,
    idempotencyKey =
      globalThis.crypto?.randomUUID?.() ??
      `runtime-v2-${Date.now()}-${Math.random().toString(16).slice(2)}`,
  ) =>
    request<RuntimeV2MessageResponse>(
      `/api/v2/conversations/${conversationId}/messages`,
      {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey },
        body: JSON.stringify({ content, laneId: laneId ?? null }),
      },
    ),
  listRuntimeV2Lanes: (conversationId: string, includeArchived = false) => {
    const params = new URLSearchParams();
    if (includeArchived) params.set("includeArchived", "true");
    const query = params.toString();
    return request<RuntimeV2LaneListResponse>(
      `/api/v2/conversations/${conversationId}/lanes${query ? `?${query}` : ""}`,
    );
  },
  createRuntimeV2Lane: (
    conversationId: string,
    input: RuntimeV2LaneCreateInput,
  ) =>
    request<RuntimeV2LaneCreateResponse>(
      `/api/v2/conversations/${conversationId}/lanes`,
      {
        method: "POST",
        body: JSON.stringify({ kind: "persistent_branch", ...input }),
      },
    ),
  renameRuntimeV2Lane: (laneId: string, displayName: string | null) =>
    request<RuntimeV2LaneUpdateResponse>(`/api/v2/lanes/${laneId}`, {
      method: "PATCH",
      body: JSON.stringify({ displayName }),
    }),
  promoteRuntimeV2Lane: (laneId: string) =>
    request<RuntimeV2LanePromoteResponse>(`/api/v2/lanes/${laneId}/promote`, {
      method: "POST",
    }),
  archiveRuntimeV2Lane: (laneId: string) =>
    request<RuntimeV2LaneTreeUpdateResponse>(`/api/v2/lanes/${laneId}/archive`, {
      method: "POST",
    }),
  restoreRuntimeV2Lane: (laneId: string) =>
    request<RuntimeV2LaneTreeUpdateResponse>(`/api/v2/lanes/${laneId}/restore`, {
      method: "POST",
    }),
  createRuntimeV2TemporaryConversation: (
    sourceConversationId: string,
    input: RuntimeV2TemporaryConversationCreateInput,
  ) =>
    request<RuntimeV2TemporaryConversationCreateResponse>(
      `/api/v2/conversations/${sourceConversationId}/temporary-conversations`,
      {
        method: "POST",
        body: JSON.stringify(input),
      },
    ),
  promoteRuntimeV2TemporaryConversation: (conversationId: string) =>
    request<RuntimeV2TemporaryConversationPromoteResponse>(
      `/api/v2/temporary-conversations/${conversationId}/promote`,
      { method: "POST" },
    ),
  deleteRuntimeV2TemporaryConversation: (conversationId: string) =>
    request<void>(`/api/v2/temporary-conversations/${conversationId}`, {
      method: "DELETE",
    }),
  listRuntimeV2RunVariants: (runId: string) =>
    request<RuntimeV2RunVariantListResponse>(`/api/v2/runs/${runId}/variants`),
  regenerateRuntimeV2Run: (runId: string) =>
    request<RuntimeV2RegenerateResponse>(`/api/v2/runs/${runId}/regenerate`, {
      method: "POST",
    }),
  selectRuntimeV2RunVariant: (runId: string) =>
    request<RuntimeV2RunSelectResponse>(`/api/v2/runs/${runId}/select`, {
      method: "POST",
    }),
  resendRuntimeV2Run: (runId: string, content: string) =>
    request<RuntimeV2RegenerateResponse>(`/api/v2/runs/${runId}/resend`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  listRuntimeV2Memories: (
    conversationId: string,
    laneId: string,
    runId?: string | null,
  ) => {
    const params = new URLSearchParams({ lane_id: laneId });
    if (runId) params.set("run_id", runId);
    return request<RuntimeV2MemoryListResponse>(
      `/api/v2/conversations/${conversationId}/memories?${params.toString()}`,
    );
  },
  createRuntimeV2Memory: (
    conversationId: string,
    laneId: string,
    kind: "preference" | "fact",
    content: string,
  ) =>
    request<RuntimeV2MemoryCreateResponse>(
      `/api/v2/conversations/${conversationId}/memories`,
      {
        method: "POST",
        body: JSON.stringify({ kind, content, laneId }),
      },
    ),
  createRuntimeV2MemoryPromotion: (
    memoryId: string,
    targetScope: RuntimeV2MemoryPromotionTarget,
    targetLaneId?: string | null,
  ) =>
    request<RuntimeV2MemoryPromotionCreateResponse>(
      `/api/v2/memories/${memoryId}/promotions`,
      {
        method: "POST",
        body: JSON.stringify({ targetScope, targetLaneId: targetLaneId ?? null }),
      },
    ),
  listRuntimeV2MemoryPromotions: (
    conversationId: string,
    includeResolved = false,
  ) =>
    request<RuntimeV2MemoryPromotionListResponse>(
      `/api/v2/conversations/${conversationId}/memory-promotions?include_resolved=${includeResolved}`,
    ),
  resolveRuntimeV2MemoryPromotion: (
    promotionId: string,
    decision: "accept" | "reject",
  ) =>
    request<RuntimeV2MemoryPromotionResolveResponse>(
      `/api/v2/memory-promotions/${promotionId}/resolve`,
      {
        method: "POST",
        body: JSON.stringify({ decision }),
      },
    ),
  steerRuntimeV2Run: (runId: string, content: string) =>
    request<{ runId: string; accepted: boolean }>(`/api/v2/runs/${runId}/steer`, {
      method: "POST",
      body: JSON.stringify({ content }),
    }),
  cancelRuntimeV2Run: (runId: string) =>
    request<{ runId: string; accepted: boolean }>(`/api/v2/runs/${runId}/cancel`, {
      method: "POST",
    }),
  resolveRuntimeV2Approval: (approvalId: string, decision: "approve" | "deny") =>
    request<{ approvalId: string; resolved: boolean }>(
      `/api/v2/approvals/${approvalId}`,
      {
        method: "POST",
        body: JSON.stringify({ decision }),
      },
    ),
  resolveRuntimeV2Recovery: (
    runId: string,
    action: "mark_failed" | "retry",
  ) =>
    request<RuntimeV2RecoveryResponse>(`/api/v2/runs/${runId}/recovery`, {
      method: "POST",
      body: JSON.stringify({ action }),
    }),
  listArtifactProposals: async (conversationId: string, includeResolved = false) => {
    const response = await request<{ items: ArtifactProposal[] }>(
      `/conversations/${conversationId}/artifact-proposals?include_resolved=${includeResolved}`,
    );
    return response.items;
  },
  resolveArtifactProposal: (proposalId: string, decision: "accept" | "reject") =>
    request<{ proposal: ArtifactProposal; artifact?: ArtifactRecordSummary }>(
      `/artifact-proposals/${proposalId}/resolve`,
      { method: "POST", body: JSON.stringify({ decision }) },
    ),
  listMemoryProposals: async (conversationId: string, includeResolved = false) => {
    const response = await request<{ items: MemoryProposal[] }>(
      `/conversations/${conversationId}/memory-proposals?include_resolved=${includeResolved}`,
    );
    return response.items;
  },
  resolveMemoryProposal: (proposalId: string, decision: "accept" | "reject") =>
    request<{ proposal: MemoryProposal }>(`/memory-proposals/${proposalId}/resolve`, {
      method: "POST",
      body: JSON.stringify({ decision }),
    }),
  listTaskProposals: async (conversationId: string, includeResolved = false) => {
    const response = await request<{ items: TaskProposal[] }>(
      `/conversations/${conversationId}/task-proposals?include_resolved=${includeResolved}`,
    );
    return response.items;
  },
  resolveTaskProposal: (proposalId: string, decision: "accept" | "reject") =>
    request<{ proposal: TaskProposal; task?: TaskSummary; reminder?: Reminder }>(
      `/task-proposals/${proposalId}/resolve`,
      { method: "POST", body: JSON.stringify({ decision }) },
    ),
  listTasks: async () => {
    const response = await request<{ items: TaskSummary[] }>("/tasks");
    return response.items;
  },
  listTaskRuns: async (taskId: string) => {
    const response = await request<{ items: TaskRun[] }>(`/tasks/${taskId}/runs`);
    return response.items;
  },
  runTask: (taskId: string) =>
    request<{ run: TaskRun }>(`/tasks/${taskId}/run`, { method: "POST" }),
  pauseTask: (taskId: string) =>
    request<{ task: TaskSummary }>(`/tasks/${taskId}/pause`, {
      method: "POST",
    }),
  resumeTask: (taskId: string) =>
    request<{ task: TaskSummary }>(`/tasks/${taskId}/resume`, {
      method: "POST",
    }),
  cancelTask: (taskId: string) =>
    request<{ task: TaskSummary }>(`/tasks/${taskId}/cancel`, {
      method: "POST",
    }),
  listNotifications: async (unreadOnly: boolean) => {
    const response = await request<{ items: TaskNotification[] }>(
      `/notifications${unreadOnly ? "?unread_only=true" : ""}`,
    );
    return response.items;
  },
  markNotificationRead: (notificationId: string) =>
    request<{ notification: TaskNotification }>(
      `/notifications/${notificationId}/read`,
      { method: "POST" },
    ),
  markAllNotificationsRead: () =>
    request<{ count: number }>("/notifications/read-all", {
      method: "POST",
    }),
  listPendingProposals: async () => {
    const response = await request<{ items: PendingProposal[] }>(
      "/proposals/pending",
    );
    return response.items;
  },
  listReminders: async () => {
    const response = await request<{ items: Reminder[] }>("/reminders");
    return response.items;
  },
  cancelReminder: (reminderId: string) =>
    request<{ reminder: Reminder }>(`/reminders/${reminderId}/cancel`, {
      method: "POST",
    }),
  getPermissionSettings: () =>
    request<PermissionSettings>("/settings/permissions"),
  setPermissionSettings: (mode: string, acknowledge: boolean) =>
    request<PermissionSettings>("/settings/permissions", {
      method: "POST",
      body: JSON.stringify({ mode, acknowledge }),
    }),
  getWorkspace: (conversationId: string) =>
    request<WorkspaceSnapshot>(`/conversations/${conversationId}/workspace`),
  getArtifact: (artifactId: string) =>
    request<ArtifactDetailSnapshot>(`/artifacts/${artifactId}`),
  listArtifactVersions: async (artifactId: string) => {
    const response = await request<{ items: ArtifactVersionRecord[] }>(
      `/artifacts/${artifactId}/versions`,
    );
    return response.items;
  },
  searchGlobal: async (
    query: string,
    scopes: string[] = ["source", "memory", "artifact", "conversation"],
    limit = 6,
  ) => {
    const response = await request<GlobalSearchResponse>("/search", {
      method: "POST",
      body: JSON.stringify({ query, scopes, limit }),
    });
    return response.groups;
  },
  listTrajectoryBundles: async () => {
    const response = await request<TrajectoryListResponse>("/api/v2/trajectory");
    return response.items;
  },
  getTrajectoryMeta: async (runId: string) =>
    request<TrajectoryMeta>(`/api/v2/trajectory/${encodeURIComponent(runId)}`),
  listSkillPackages: async (workspaceId?: string | null) => {
    const parameters = new URLSearchParams();
    if (workspaceId) parameters.set("workspaceId", workspaceId);
    const suffix = parameters.toString();
    return request<SkillPackagesResponse>(
      `/api/v2/skills/packages${suffix ? `?${suffix}` : ""}`,
    );
  },
  listKnowledgeSources: async (
    status = "active",
    workspace?: string | null,
  ) => {
    const parameters = new URLSearchParams({ status });
    if (workspace !== undefined) {
      parameters.set("workspace", workspace ?? "general");
    }
    const response = await request<{ items: KnowledgeSource[] }>(
      `/knowledge-sources?${parameters.toString()}`,
    );
    return response.items;
  },
  createKnowledgeSource: (body: {
    kind: "file" | "note";
    title: string;
    content: string;
    fileName?: string;
    expiresAt?: string;
    workspaceId?: string | null;
  }) =>
    request<{ source: KnowledgeSource }>("/knowledge-sources", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateKnowledgeSource: (
    sourceId: string,
    body: {
      title?: string;
      content?: string;
      fileName?: string;
      expiresAt?: string;
    },
  ) =>
    request<{ source: KnowledgeSource }>(`/knowledge-sources/${sourceId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  expireKnowledgeSource: (sourceId: string) =>
    request<{ source: KnowledgeSource }>(
      `/knowledge-sources/${sourceId}/expire`,
      { method: "POST" },
    ),
  restoreKnowledgeSource: (sourceId: string) =>
    request<{ source: KnowledgeSource }>(
      `/knowledge-sources/${sourceId}/restore`,
      { method: "POST" },
    ),
  deleteKnowledgeSource: (sourceId: string) =>
    request<void>(`/knowledge-sources/${sourceId}`, { method: "DELETE" }),
  importKnowledgeFile: async (
    file: File,
    title?: string,
    workspaceId?: string | null,
  ) => {
    const form = new FormData();
    form.append("file", file);
    if (title?.trim()) form.append("title", title.trim());
    form.append("workspaceId", workspaceId === null ? "general" : (workspaceId ?? "general"));
    const response = await fetch(url("/knowledge-sources/import"), {
      method: "POST",
      headers: { Accept: "application/json" },
      body: form,
    });
    if (!response.ok) {
      let payload: ApiErrorEnvelope | undefined;
      try {
        payload = (await response.json()) as ApiErrorEnvelope;
      } catch {
        payload = undefined;
      }
      throw new ApiClientError(response, payload);
    }
    return (await response.json()) as {
      source: KnowledgeSource;
      truncated: boolean;
      encoding: string;
    };
  },
  recordCitationClick: (body: {
    label: string;
    scope: string;
    refId: string;
    turnId?: string;
    conversationId?: string;
  }) =>
    request<{ id: string }>("/retrieval-events", {
      method: "POST",
      body: JSON.stringify({ kind: "citation_click", ...body }),
    }),
  listKnowledgeProposals: async (conversationId: string, includeResolved = false) => {
    const response = await request<{ items: KnowledgeProposal[] }>(
      `/conversations/${conversationId}/knowledge-proposals?include_resolved=${includeResolved}`,
    );
    return response.items;
  },
  resolveKnowledgeProposal: (
    proposalId: string,
    decision: "accept" | "reject",
    workspaceId?: string | null,
  ) =>
    request<{ proposal: KnowledgeProposal; source?: KnowledgeSource }>(
      `/knowledge-proposals/${proposalId}/resolve`,
      {
        method: "POST",
        body: JSON.stringify({
          decision,
          ...(decision === "accept" && workspaceId !== undefined
            ? { workspaceId: workspaceId === null ? "general" : workspaceId }
            : {}),
        }),
      },
    ),
  listMemories: async (includeDeleted = false) => {
    const response = await request<{ items: MemoryRecord[] }>(
      `/memories?include_deleted=${includeDeleted}`,
    );
    return response.items;
  },
  updateMemory: (memoryId: string, content: string) =>
    request<{ memory: MemoryRecord }>(`/memories/${memoryId}`, {
      method: "PATCH",
      body: JSON.stringify({ content }),
    }),
  deleteMemory: (memoryId: string) =>
    request<void>(`/memories/${memoryId}`, { method: "DELETE" }),
  rollbackArtifact: (
    artifactId: string,
    body: {
      targetOrdinal: number;
      sourceConversationId: string;
      sourceTurnId: string;
      note?: string;
    },
  ) =>
    request<{ artifact: ArtifactRecordSummary; currentVersion: ArtifactVersionRecord }>(
      `/artifacts/${artifactId}/rollback`,
      { method: "POST", body: JSON.stringify(body) },
    ),
};

export type ExportFormat = "markdown" | "html" | "pdf";

function parseContentDispositionFilename(header: string | null): string | null {
  if (!header) return null;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(header);
  if (star?.[1]) {
    try {
      return decodeURIComponent(star[1].trim());
    } catch {
      return null;
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(header);
  return plain?.[1]?.trim() ?? null;
}

export async function downloadArtifactExport(
  artifactId: string,
  format: ExportFormat,
): Promise<void> {
  const response = await fetch(
    url(`/artifacts/${artifactId}/export?format=${format}`),
    { headers: { Accept: "*/*" } },
  );
  if (!response.ok) throw await responseError(response);
  const blob = await response.blob();
  const filename =
    parseContentDispositionFilename(response.headers.get("Content-Disposition")) ??
    `artifact-${artifactId}`;
  const objectUrl = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = objectUrl;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(objectUrl);
}

export async function getRuntimeV2RunUsageCost(
  runId: string,
): Promise<RuntimeV2RunUsageCost> {
  return request<RuntimeV2RunUsageCost>(`/api/v2/runs/${runId}/usage-cost`);
}

export async function fetchRuntimeV2Metrics(): Promise<RuntimeV2Metrics> {
  return request<RuntimeV2Metrics>("/api/v2/metrics");
}

export async function streamRuntimeV2Events(options: {
  conversationId: string;
  laneId?: string | null;
  afterSequence: number;
  signal: AbortSignal;
  onSnapshot: (snapshot: RuntimeV2Snapshot) => void;
  onProductEvent: (event: RuntimeV2ProductEvent) => void;
}): Promise<void> {
  const streamParams = new URLSearchParams({
    after_seq: String(options.afterSequence),
  });
  if (options.laneId) streamParams.set("lane_id", options.laneId);
  const response = await fetch(
    url(
      `/api/v2/conversations/${options.conversationId}/events?${streamParams.toString()}`,
    ),
    {
      headers: { Accept: "text/event-stream" },
      signal: options.signal,
    },
  );
  if (!response.ok) {
    throw await responseError(response);
  }
  if (!response.body) {
    throw new Error("浏览器无法读取流式响应。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      const eventName = frame
        .split("\n")
        .find((line) => line.startsWith("event:"))
        ?.slice(6)
        .trim();
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (eventName && data) {
        const payload = JSON.parse(data);
        if (eventName === "conversation.snapshot_ready") {
          options.onSnapshot(payload as RuntimeV2Snapshot);
        } else {
          options.onProductEvent(payload as RuntimeV2ProductEvent);
        }
      }
      boundary = buffer.indexOf("\n\n");
    }
    if (done) return;
  }
}
