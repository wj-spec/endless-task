import type { KnowledgeProposal,
  CompactTurnSnapshot,
  PendingProposal,
  ApprovalRequest,
  ArtifactDetailSnapshot,
  ArtifactProposal,
  ArtifactRecordSummary,
  ArtifactVersionRecord,
  Conversation,
  ConversationSnapshot,
  ConversationStatus,
  HealthSnapshot,
  MemoryProposal,
  KnowledgeSource,
  KnowledgeCitation,
  MemoryRecord,
  PermissionSettings,
  RuntimeEvent,
  TurnCommandResponse,
  UploadedTextFile,
  BrowseItem,
  EffectLogEntry,
  Workspace,
  WorkspaceSnapshot,
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
  createConversation: (workspaceId?: string | null) =>
    request<Conversation>("/conversations", {
      method: "POST",
      body: JSON.stringify({
        workspaceId: workspaceId === null ? "general" : workspaceId,
      }),
    }),
  listWorkspaces: async () => {
    const response = await request<{ items: Workspace[] }>("/workspaces");
    return response.items;
  },
  createWorkspace: async (name: string) => {
    const response = await request<{ workspace: Workspace }>("/workspaces", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    return response.workspace;
  },
  patchWorkspace: (workspaceId: string, rootPath: string | null) =>
    request<{ workspace: Workspace }>(`/workspaces/${workspaceId}`, {
      method: "PATCH",
      body: JSON.stringify({ rootPath }),
    }).then((response) => response.workspace),
  browseFilesystem: async (path?: string, showHidden = false) => {
    const parameters = new URLSearchParams();
    if (path) parameters.set("path", path);
    if (showHidden) parameters.set("showHidden", "true");
    const response = await request<{ currentPath: string; items: BrowseItem[] }>(
      `/filesystem/browse?${parameters.toString()}`,
    );
    return response;
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
    patch: { title?: string; status?: ConversationStatus },
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
  createTurn: (conversationId: string, content: string, idempotencyKey: string) =>
    request<TurnCommandResponse>(`/conversations/${conversationId}/turns`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ content }),
    }),
  getTurn: (turnId: string) => request<CompactTurnSnapshot>(`/turns/${turnId}`),
  cancelTurn: (turnId: string) =>
    request<CompactTurnSnapshot>(`/turns/${turnId}/cancel`, { method: "POST" }),
  retryTurn: (turnId: string, idempotencyKey: string) =>
    request<TurnCommandResponse>(`/turns/${turnId}/retry`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  regenerateTurn: (turnId: string, idempotencyKey: string) =>
    request<TurnCommandResponse>(`/turns/${turnId}/regenerate`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    }),
  selectVariant: (turnId: string, variantId: string) =>
    request<TurnCommandResponse>(`/turns/${turnId}/response-variants/${variantId}/select`, {
      method: "POST",
    }),
  resolveApproval: (approvalId: string, decision: "approve" | "deny") =>
    request<ApprovalRequest>(`/approvals/${approvalId}`, {
      method: "POST",
      body: JSON.stringify({ decision }),
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
  getTurnCitations: async (turnId: string) => {
    const response = await request<{ items: KnowledgeCitation[] }>(
      `/turns/${turnId}/citations`,
    );
    return response.items;
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

export async function streamTurnEvents(options: {
  turnId: string;
  afterSequence: number;
  signal: AbortSignal;
  onEvent: (event: RuntimeEvent) => void;
}): Promise<void> {
  const headers: Record<string, string> = { Accept: "text/event-stream" };
  if (options.afterSequence > 0) {
    headers["Last-Event-ID"] = `${options.turnId}:${options.afterSequence}`;
  }
  const response = await fetch(url(`/turns/${options.turnId}/events`), {
    headers,
    signal: options.signal,
  });
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
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (data) options.onEvent(JSON.parse(data) as RuntimeEvent);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) return;
  }
}
