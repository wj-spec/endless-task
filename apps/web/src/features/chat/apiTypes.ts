export type ConversationStatus = "active" | "archived";
export type ConversationKind = "normal" | "ephemeral";
export type TurnStatus = "created" | "running" | "completed" | "failed" | "cancelled";
export type ResponseVariantStatus = TurnStatus;
export type ResponseVariantOperation = "create" | "retry" | "regenerate";

export type Workspace = {
  id: string;
  name: string;
  rootPath: string | null;
  createdAt: string;
  updatedAt: string;
};

export type WorkspaceFilePreviewLine = {
  line: number;
  text: string;
};

export type WorkspaceFilePreview = {
  path: string;
  workspaceId: string;
  totalLines: number;
  startLine: number;
  endLine: number;
  variant: string;
  lines: WorkspaceFilePreviewLine[];
};

export type BrowseItem = {
  name: string;
  path: string;
  kind: "directory" | "file";
  size: number;
  writable: boolean;
  isHidden: boolean;
};

export type SkillDiagnostic = {
  code: string;
  message: string;
  path: string;
};

export type Skill = {
  name: string;
  description: string;
  scope: "user" | "workspace";
  filePath: string;
  disabled: boolean;
  disableModelInvocation: boolean;
  diagnostics: SkillDiagnostic[];
};

export type McpToolStatus = {
  publicName: string;
  rawName: string;
  description: string;
  effect: "read_only" | "local_write" | "external_action";
  requiresExplicitConfirmation: boolean;
};

export type McpServer = {
  id: string;
  name: string;
  transport: "stdio" | "http";
  command: string;
  args: string[];
  cwd: string;
  url: string;
  enabled: boolean;
  toolCallTimeoutSeconds: number;
  state:
    | "connected"
    | "reconnecting"
    | "disconnected"
    | "disabled"
    | "error";
  toolCount: number;
  lastError: string | null;
  tools: McpToolStatus[];
  createdAt: string;
  updatedAt: string;
};

export type McpServerInput = {
  name: string;
  transport: "stdio" | "http";
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  cwd?: string;
  url?: string;
  headers?: Record<string, string>;
  enabled?: boolean;
  toolCallTimeoutSeconds?: number;
};

export type EffectLogEntry = {
  time: string;
  conversationId: string;
  workspaceId: string;
  workspaceRoot: string;
  operation: string;
  detail: string;
  receipt: {
    kind: string;
    path: string;
    sha256: string;
    executedAt: string;
    exitCode: number | null;
    timedOut: boolean;
    truncated: boolean;
    unknownOutcome: boolean;
  };
  approver: string;
  durationMs: number;
};

export type Conversation = {
  id: string;
  title: string;
  status: ConversationStatus;
  nextTurnOrdinal: number;
  titleIsManual: boolean;
  createdAt: string;
  updatedAt: string;
  archivedAt: string | null;
  parentConversationId: string | null;
  forkTurnId: string | null;
  kind: ConversationKind;
  providerProfileId: string | null;
  modelOverride: string | null;
  promotedAt: string | null;
  workspaceId: string | null;
};

export type ProviderModel = {
  providerProfileId: string;
  modelId: string;
  displayName: string;
  source: "discovered" | "manual";
  enabled: boolean;
  isDefault: boolean;
  lastSeenAt: string | null;
};

export type ProviderProfile = {
  id: string;
  name: string;
  kind: string;
  baseUrl: string;
  defaultModel: string;
  timeoutSeconds: number;
  enabled: boolean;
  isBuiltin: boolean;
  isDefault: boolean;
  configured: boolean;
  apiKeyConfigured: boolean;
  connectionState: "untested" | "ready" | "failed";
  lastCheckedAt: string | null;
  lastError: string | null;
  models: ProviderModel[];
  createdAt: string;
  updatedAt: string;
};

export type ProviderProfileInput = {
  name: string;
  defaultModel?: string;
  baseUrl?: string;
  apiKey?: string;
  apiKeyRef?: string;
  timeoutSeconds?: number;
  enabled?: boolean;
};

export type Message = {
  id: string;
  conversationId: string;
  turnId: string;
  role: "user" | "assistant";
  content: string;
  createdAt: string;
  updatedAt: string;
};

export type Turn = {
  id: string;
  conversationId: string;
  ordinal: number;
  userMessageId: string;
  activeResponseVariantId: string | null;
  status: TurnStatus;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  inherited?: boolean;
};

export type ResponseVariant = {
  id: string;
  turnId: string;
  assistantMessageId: string;
  index: number;
  operation: ResponseVariantOperation;
  status: ResponseVariantStatus;
  provider: string | null;
  model: string | null;
  finishReason: "stop" | "length" | "content_filter" | "error" | "cancelled" | null;
  errorCode: string | null;
  inputTokens: number | null;
  outputTokens: number | null;
  createdAt: string;
  startedAt: string | null;
  finishedAt: string | null;
};

export type ResponseVariantSnapshot = {
  variant: ResponseVariant;
  assistantMessage: Message;
};

export type ConversationTurnSnapshot = {
  turn: Turn;
  userMessage: Message;
  activeResponseVariantId: string | null;
  responseVariants: ResponseVariantSnapshot[];
  activities: ActivitySnapshot[];
};

export type ConversationSnapshot = {
  conversation: Conversation;
  turns: ConversationTurnSnapshot[];
  files: UploadedTextFile[];
  parentTitle?: string | null;
};

export type UploadedTextFile = {
  id: string;
  conversationId: string;
  originalName: string;
  mediaType: string;
  byteSize: number;
  sha256: string;
  createdAt: string;
};

export type RuntimeErrorDetail = {
  code: string;
  message: string;
  retryable: boolean;
  retryAfterMs?: number;
  correlationId: string;
};

export type ApprovalStatus = "pending" | "approved" | "denied" | "cancelled" | "expired";

export type ApprovalRequest = {
  id: string;
  toolCallId: string;
  summary: string;
  reason: string;
  status: ApprovalStatus;
  createdAt: string;
  resolvedAt: string | null;
  metadata: Record<string, unknown>;
};

export type ActivityStatus = "running" | "completed" | "failed" | "cancelled";

export type ActivitySnapshot = {
  id: string;
  status: ActivityStatus;
  message: string;
  startedAt: string;
  updatedAt: string;
};

export type HealthSnapshot = {
  status: string;
  provider: string;
  model: string;
  providerConfigured: boolean;
};

export type CapabilityState = "ok" | "degraded" | "unavailable";

export type CapabilitySummary = {
  state: CapabilityState;
  issues: string[];
};

export type CapabilitySkillStatus = {
  state: CapabilityState;
  total: number;
  enabled: number;
  diagnostics: SkillDiagnostic[];
};

export type CapabilityMcpServerStatus = {
  id: string;
  name: string;
  state: string;
  toolCount: number;
  lastError: string | null;
};

export type CapabilityMcpStatus = {
  state: CapabilityState;
  servers: CapabilityMcpServerStatus[];
};

export type CapabilityProviderStatus = {
  state: CapabilityState;
  defaultProfileId: string;
  profileName: string;
  model: string;
  configured: boolean;
  fallback: boolean;
};

export type CapabilityEmbeddingStatus = {
  state: CapabilityState;
  enabled: boolean;
  backend: string | null;
  ready: boolean;
};

export type CapabilitySnapshot = {
  summary: CapabilitySummary;
  skills: CapabilitySkillStatus;
  mcp: CapabilityMcpStatus;
  provider: CapabilityProviderStatus;
  embedding: CapabilityEmbeddingStatus;
  delegation?: CapabilityDelegationStatus;
};

export type CapabilityDelegationStatus = {
  enabled: boolean;
  mode: "readonly" | null;
};

export type RuntimeV2RunUsageCost = {
  runId: string;
  rows: {
    provider: string;
    model: string;
    inputTokens: number;
    outputTokens: number;
    requestCount: number;
    costUsd: number;
  }[];
  totals: {
    inputTokens: number;
    outputTokens: number;
    requestCount: number;
    costUsd: number;
  };
};

export type RuntimeV2Metrics = {
  runs: {
    count: number;
    byStatus: Record<string, number>;
    avgDurationMs: number | null;
    totalInputTokens: number;
    totalOutputTokens: number;
    compactedRuns: number;
    compactionReleasedTokens: number;
  };
  modelTurns: {
    count: number;
    avgFirstTokenLatencyMs: number | null;
    avgDurationMs: number | null;
    finishReasons: Record<string, number>;
  };
  approvals: {
    count: number;
    avgWaitMs: number | null;
    decisions: Record<string, number>;
  };
  compactions: { events: number };
  prefixStability: { comparisons: number; stableRate: number | null };
};

export type LiveTurn = {
  turnId: string;
  responseVariantId: string;
  status: TurnStatus;
  content: string;
  lastSequence: number;
  error?: RuntimeErrorDetail;
  pendingApproval?: ApprovalRequest;
  activities: ActivitySnapshot[];
};

export type RuntimeV2Entry = {
  id: string;
  type: string;
  actor: string;
  status: string;
  createdAt: string;
  sourceRunId: string | null;
  inherited?: boolean;
  data: {
    content?: string;
    toolName?: string;
    arguments?: unknown;
    errorCode?: string | null;
    trustLevel?: string;
    reference?: unknown;
    /** 工具执行 id / 模型工具调用 id，用于在前端可靠地把 调用参数 与 结果 关联起来。 */
    callId?: string | null;
    toolExecutionId?: string | null;
    /** 工具返回的结构化内容（如 run_shell 的 exitCode/stdout，read 的行号等），用于类型化展示。 */
    structuredContent?: unknown;
  };
};

export type RuntimeV2ToolState = {
  id: string;
  runId: string;
  modelTurnId: string;
  callId: string;
  toolName: string;
  status: string;
  errorCode: string | null;
  safeMessage: string | null;
  retryable: boolean | null;
  correlationId: string | null;
  errorDetails: Record<string, unknown> | null;
  resultEntryId: string | null;
};

export type RuntimeV2Approval = {
  id: string;
  runId: string;
  modelTurnId: string;
  toolExecutionId: string;
  toolName: string;
  summary: string;
  reason: string;
  /** 风险等级（低/中/高），用于前端风险色提示。 */
  effect?: string | null;
  risk?: "low" | "medium" | "high" | null;
};

export type RuntimeV2RecoveryReport = {
  runId: string;
  status: string;
  classification: string;
  action: string;
  findings: Array<{
    reason: string;
    message: string;
    modelTurnId: string | null;
    toolExecutionId: string | null;
  }>;
};

export type RuntimeV2RunState = {
  runId: string;
  status: string;
  partialContent: string;
  errorCode: string | null;
  safeMessage: string | null;
  modelTurns: Array<{
    id: string;
    index: number;
    status: string;
    partialContent: string;
    inputTokens: number | null;
    outputTokens: number | null;
    toolExecutions: RuntimeV2ToolState[];
  }>;
};

export type RuntimeV2Snapshot = {
  snapshotVersion: number;
  conversationId: string;
  activeLaneId: string | null;
  mainLaneId: string | null;
  runningLaneId: string | null;
  runningRunId: string | null;
  activeRunId: string | null;
  activeRunVariantId: string | null;
  lastEventSeq: number;
  entries: RuntimeV2Entry[];
  runState: RuntimeV2RunState | null;
  pendingApprovals: RuntimeV2Approval[];
  toolStates: RuntimeV2ToolState[];
  contextUsage: {
    inputTokens: number;
    outputTokens: number;
  };
  contextBudget?: {
    limitTokens: number;
    usedTokens: number;
    usedRatio: number;
    remainingTokens: number;
  };
  interruptedRuns: RuntimeV2RecoveryReport[];
  capabilities: string[];
};

export type RuntimeV2LaneKind =
  | "main"
  | "persistent_branch"
  | "temporary"
  | "archived";

export type RuntimeV2LaneStatus = "active" | "archived";

export type RuntimeV2Lane = {
  id: string;
  conversationId: string;
  kind: RuntimeV2LaneKind;
  status: RuntimeV2LaneStatus;
  archived: boolean;
  archivedAt: string | null;
  displayName: string | null;
  summary: string | null;
  title: string | null;
  baseEntryExcerpt: string | null;
  baseEntryId: string | null;
  leafEntryId: string | null;
  createdFromEntryId: string | null;
  createdAt: string;
  sourceLaneId: string | null;
  isMain: boolean;
};

export type RuntimeV2LaneListResponse = {
  conversationId: string;
  activeLaneId: string | null;
  mainLaneId: string | null;
  runningLaneId: string | null;
  runningRunId: string | null;
  items: RuntimeV2Lane[];
};

export type RuntimeV2LaneCreateInput = {
  sourceLaneId: string;
  baseEntryId?: string;
  displayName?: string;
};

export type RuntimeV2LaneCreateResponse = {
  lane: RuntimeV2Lane;
  sourceLane: RuntimeV2Lane;
  baseEntryId: string;
};

export type RuntimeV2LaneUpdateResponse = {
  lane: RuntimeV2Lane;
};

export type RuntimeV2LaneTreeUpdateResponse = {
  items: RuntimeV2Lane[];
};

export type RuntimeV2LanePromoteResponse = {
  lane: RuntimeV2Lane;
  previousMainLane: RuntimeV2Lane;
  activeLaneId: string;
};

export type RuntimeV2TemporaryConversationCreateInput = {
  sourceLaneId?: string;
  sourceLeafEntryId?: string;
  title?: string;
};

export type RuntimeV2TemporaryConversationCreateResponse = {
  conversation: Conversation;
  lane: RuntimeV2Lane;
};

export type RuntimeV2TemporaryConversationPromoteResponse = {
  conversation: Conversation;
};

export type RuntimeV2RunVariant = {
  runId: string;
  conversationId: string;
  laneId: string;
  triggerEntryId: string;
  siblingGroupId: string;
  assistantEntryId: string | null;
  status: string;
  isActiveVariant: boolean;
  createdAt: string;
  finishedAt: string | null;
};

export type RuntimeV2RunVariantListResponse = {
  runId: string;
  siblingGroupId: string | null;
  items: RuntimeV2RunVariant[];
};

export type RuntimeV2RegenerateResponse = {
  oldRunId: string;
  newRunId: string;
  laneId: string;
  triggerEntryId: string;
  siblingGroupId: string;
};

export type RuntimeV2RunSelectResponse = {
  runId: string;
  assistantEntryId: string | null;
  isActiveVariant: boolean;
};

export type RuntimeV2Memory = {
  id: string;
  scope:
    | "user_global"
    | "workspace"
    | "conversation_tree"
    | "branch"
    | "temporary"
    | "run_scratch";
  kind: "preference" | "fact";
  content: string;
  status: string;
  conversationId: string;
  workspaceId: string | null;
  laneId: string | null;
  runId: string | null;
  sourceMemoryId: string | null;
  sourceEntryId: string | null;
  createdAt: string;
  updatedAt: string;
};

export type RuntimeV2MemoryListResponse = {
  conversationId: string;
  laneId: string;
  items: RuntimeV2Memory[];
};

export type RuntimeV2MemoryCreateResponse = {
  memory: RuntimeV2Memory;
};

export type RuntimeV2MemoryPromotionTarget =
  | "user_global"
  | "workspace"
  | "conversation_tree"
  | "branch";

export type RuntimeV2MemoryPromotion = {
  id: string;
  memoryId: string;
  targetScope: RuntimeV2MemoryPromotionTarget;
  targetWorkspaceId: string | null;
  targetLaneId: string | null;
  status: "pending" | "accepted" | "rejected" | "cancelled";
  resolvedMemoryId: string | null;
  conflictMemoryId: string | null;
  createdAt: string;
  updatedAt: string;
  resolvedAt: string | null;
};

export type RuntimeV2MemoryPromotionListResponse = {
  conversationId: string;
  items: RuntimeV2MemoryPromotion[];
};

export type RuntimeV2MemoryPromotionCreateResponse = {
  promotion: RuntimeV2MemoryPromotion;
};

export type RuntimeV2MemoryPromotionResolveResponse = {
  promotion: RuntimeV2MemoryPromotion;
  memory: RuntimeV2Memory | null;
};

export type RuntimeV2ProductEvent = {
  eventId: string;
  eventSeq: number;
  type: string;
  conversationId: string;
  laneId: string | null;
  runId: string | null;
  createdAt: string;
  data: {
    runId?: string;
    modelTurnId?: string;
    toolExecutionId?: string;
    approvalId?: string;
    delta?: string;
    status?: string;
    decision?: string;
    errorCode?: string | null;
    safeMessage?: string | null;
    retryable?: boolean | null;
    correlationId?: string | null;
    errorDetails?: Record<string, unknown> | null;
    resultEntryId?: string | null;
    callId?: string | null;
    toolName?: string;
    arguments?: unknown;
    content?: string;
    [key: string]: unknown;
  };
};

export type HubV2Event = {
  eventId: string;
  eventSeq: number;
  type: string;
  conversationId: string | null;
  createdAt: string;
  data: {
    kind?: string;
    conversationId?: string;
    count?: number;
    proposalId?: string;
    decision?: string;
    id?: string;
    title?: string;
    taskId?: string;
    runId?: string;
    [key: string]: unknown;
  };
};

export type RuntimeV2MessageResponse = {
  conversationId: string;
  laneId: string;
  runId: string;
  userMessageId: string;
};

export type RuntimeV2RecoveryResponse = {
  runId: string;
  action: "mark_failed" | "retry";
  laneId: string;
  newRunId: string | null;
};

export type ArtifactProposalStatus = "pending" | "accepted" | "rejected" | "cancelled";

export type ArtifactKind = "markdown" | "text";

export type ArtifactProposal = {
  id: string;
  conversationId: string;
  turnId: string;
  title: string;
  kind: ArtifactKind;
  content: string;
  reason: string;
  status: ArtifactProposalStatus;
  sourceLabels: string[];
  targetArtifactId: string | null;
  baseVersionOrdinal: number | null;
  createdAt: string;
  updatedAt: string;
  resolvedArtifactId: string | null;
  resolvedAt: string | null;
};

export type ArtifactRecordSummary = {
  id: string;
  title: string;
  kind: ArtifactKind;
  status: "active" | "deleted";
  currentVersionOrdinal: number;
  createdAt: string;
  updatedAt: string;
  deletedAt: string | null;
  storagePath?: string;
  contentSha256?: string;
};

export type MemoryProposalStatus = "pending" | "accepted" | "rejected" | "cancelled";

export type MemoryProposal = {
  id: string;
  conversationId: string;
  turnId: string;
  kind: string;
  content: string;
  reason: string;
  status: MemoryProposalStatus;
  createdAt: string;
  updatedAt: string;
  resolvedMemoryId: string | null;
  resolvedAt: string | null;
};

export type WorkspaceSnapshot = {
  conversationId: string;
  visible: boolean;
  artifacts: ArtifactRecordSummary[];
  pendingProposals: ArtifactProposal[];
};

export type TaskSchedule = {
  kind: "daily" | "weekly" | "monthly" | "once";
  time?: string;
  at?: string;
  weekday?: number;
  day?: number;
};

export type Reminder = {
  id: string;
  title: string;
  commitment: string;
  dueAt: string;
  status: "pending" | "fired" | "cancelled";
  sourceConversationId: string;
  sourceTurnId: string;
  createdAt: string;
  firedAt: string | null;
  cancelledAt: string | null;
};

export type TaskProposalStatus =
  | "pending"
  | "accepted"
  | "rejected"
  | "cancelled";

export type TaskProposal = {
  id: string;
  conversationId: string;
  turnId: string;
  title: string;
  commitment: string;
  schedule: TaskSchedule;
  scheduleDescription: string;
  reason: string;
  status: TaskProposalStatus;
  createdAt: string;
  updatedAt: string;
  resolvedTaskId: string | null;
  resolvedAt: string | null;
};

export type TaskSummary = {
  id: string;
  title: string;
  commitment: string;
  schedule: TaskSchedule;
  scheduleDescription: string;
  status: "active" | "paused" | "cancelled";
  sourceConversationId: string;
  sourceTurnId: string;
  sourceProposalId: string | null;
  createdAt: string;
  updatedAt: string;
  cancelledAt: string | null;
};

export type TaskRun = {
  id: string;
  taskId: string;
  trigger: "manual" | "scheduled";
  status: "running" | "completed" | "failed" | "cancelled";
  conversationId: string;
  turnId: string | null;
  error: string | null;
  startedAt: string;
  finishedAt: string | null;
  awaitingUser: boolean;
  awaitingNote: string | null;
  attempt: number;
  retryable: boolean;
};

export type TaskNotification = {
  id: string;
  kind: "run_completed" | "run_failed" | "run_awaiting";
  taskId: string;
  runId: string;
  conversationId: string;
  title: string;
  body: string;
  createdAt: string;
  readAt: string | null;
};

export type PendingProposal = {
  id: string;
  kind: "artifact" | "task" | "memory" | "knowledge";
  conversationId: string;
  title: string;
  createdAt: string;
};

export type ArtifactVersionOperation = "create" | "update" | "chat_continue" | "rollback";

export type SourceReference = {
  label: string;
  type: string;
  resolved: boolean;
  fileId?: string;
  fileName?: string;
  lineRange?: [number, number];
  memoryId?: string;
  memorySnippet?: string;
};

export type ArtifactVersionRecord = {
  id: string;
  artifactId: string;
  ordinal: number;
  content: string;
  operation: ArtifactVersionOperation;
  sourceConversationId: string;
  sourceTurnId: string;
  sourceLabels: string[];
  note: string | null;
  createdAt: string;
  sourceReferences?: SourceReference[];
};

export type ArtifactDetailSnapshot = {
  artifact: ArtifactRecordSummary;
  currentVersion: ArtifactVersionRecord;
};

export type MemoryStatus = "active" | "expired" | "deleted";

export type KnowledgeProposalType =
  | "add_source"
  | "expire_source"
  | "merge_source";

export type KnowledgeProposal = {
  id: string;
  conversationId: string;
  turnId: string;
  type: KnowledgeProposalType;
  payload: {
    title?: string;
    content?: string;
    reason?: string;
    source_id?: string;
    target_id?: string;
    target_title?: string;
    overlap?: number;
    decay?: boolean;
    workspace_id?: string | null;
  };
  status: "pending" | "accepted" | "rejected" | "cancelled";
  createdAt: string;
  updatedAt: string;
  resolvedSourceId: string | null;
  resolvedAt: string | null;
};

export type KnowledgeSourceStatus = "active" | "expired" | "deleted";

export type KnowledgeSource = {
  id: string;
  kind: "file" | "note";
  origin: "user" | "agent";
  title: string;
  content: string;
  fileName: string | null;
  status: KnowledgeSourceStatus;
  sourceConversationId: string | null;
  userEditedAt: string | null;
  expiresAt: string | null;
  expiredAt: string | null;
  createdAt: string;
  updatedAt: string;
  fileSize: number | null;
  fileSha256: string | null;
  workspaceId: string | null;
};

export type KnowledgeCitation = {
  label: string;
  scope: "source" | "memory" | "artifact" | "conversation";
  refId: string;
  title: string;
  snippet?: string;
  conversationId?: string;
  sourceId?: string;
  chunkSeq?: number;
  sourceQuality?: {
    recencyDays: number | null;
    authority: "高" | "中" | "低";
    relevance: number | null;
  };
};

export type ConversationCitationsResponse = {
  turnCitations: Record<string, KnowledgeCitation[]>;
};

export type FeedbackRating = "up" | "down";

export type ResponseFeedback = {
  id: string;
  conversationId: string;
  turnId: string;
  variantId: string | null;
  rating: FeedbackRating;
  reason: string | null;
  note: string | null;
  createdAt: string;
  updatedAt: string;
};

export type ResponseFeedbackInput = {
  rating: FeedbackRating;
  reason?: string;
  note?: string;
  variantId?: string;
  conversationId?: string;
};

export type MemoryRecord = {
  id: string;
  kind: string;
  content: string;
  status: MemoryStatus;
  sourceConversationId: string;
  sourceConversationTitle: string | null;
  sourceTurnId: string;
  writeOrigin: string;
  createdAt: string;
  updatedAt: string;
  sourceProposalId: string | null;
  expiredReason: string | null;
  supersededBy: string | null;
};

export type PermissionMode = "confirm_every_time" | "trust_local_writes" | "trust_all";

export type PermissionSettings = {
  mode: PermissionMode;
  updatedAt: string;
};

export type GlobalSearchScope = "source" | "memory" | "artifact" | "conversation";

export type GlobalSearchHit = {
  refId: string;
  title: string;
  snippet: string;
  updatedAt?: string | null;
  conversationId?: string | null;
  sourceId?: string | null;
  chunkSeq?: number | null;
  kind?: string | null;
  origin?: string | null;
};

export type GlobalSearchGroup = {
  scope: GlobalSearchScope;
  hits: GlobalSearchHit[];
};

export type GlobalSearchResponse = {
  groups: GlobalSearchGroup[];
};

export type SkillPackageSummary = {
  scope: string;
  name: string;
  version: string;
  state: string;
  valid: boolean;
  quarantined: boolean;
  invocable: boolean;
  requiredTools: string[];
  requiredCapabilities: string[];
};

export type SkillPackagesResponse = {
  enabled: boolean;
  mode: "legacy" | "packages";
  packages: SkillPackageSummary[];
  conflicts: { code: string; message: string }[];
};

export type TrajectoryBundleFile = { name: string; size: number };

export type TrajectoryBundleSummary = {
  runId: string;
  files: TrajectoryBundleFile[];
};

export type TrajectoryListResponse = {
  root: string;
  items: TrajectoryBundleSummary[];
};

export type TrajectoryMeta = {
  runId: string;
  fileNames: string[];
  manifest: Record<string, unknown>;
};

export type EvalBatchSummary = {
  id: string;
  mode: string;
  status: string;
  runCount: number;
  createdAt: string;
  judgeProvider: string | null;
  judgeModel: string | null;
  aggregate: Record<string, unknown> | null;
};

export type EvalBatchListResponse = { items: EvalBatchSummary[] };

export type EvalBatchDetail = EvalBatchSummary & { resultCount: number };

export type RuntimeV2Span = {
  spanId: string;
  parentSpanId: string | null;
  kind: string;
  name: string;
  status: string;
  startedAt: string;
  endedAt: string | null;
  durationMs: number | null;
  diagnosticCode: string | null;
  diagnosticMessage: string | null;
};

export type RuntimeV2SpansResponse = {
  runId: string;
  available: boolean;
  spans: RuntimeV2Span[];
};
