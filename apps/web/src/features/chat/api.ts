import type {
  CompactTurnSnapshot,
  ApprovalRequest,
  ArtifactDetailSnapshot,
  ArtifactProposal,
  ArtifactRecordSummary,
  Conversation,
  ConversationSnapshot,
  ConversationStatus,
  HealthSnapshot,
  MemoryProposal,
  RuntimeEvent,
  TurnCommandResponse,
  UploadedTextFile,
  WorkspaceSnapshot,
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
  listConversations: async (status: ConversationStatus, query?: string) => {
    const parameters = new URLSearchParams({ status });
    if (query?.trim()) parameters.set("query", query.trim());
    const response = await request<{ items: Conversation[] }>(
      `/conversations?${parameters.toString()}`,
    );
    return response.items;
  },
  createConversation: () =>
    request<Conversation>("/conversations", { method: "POST" }),
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
  getWorkspace: (conversationId: string) =>
    request<WorkspaceSnapshot>(`/conversations/${conversationId}/workspace`),
  getArtifact: (artifactId: string) =>
    request<ArtifactDetailSnapshot>(`/artifacts/${artifactId}`),
};

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
