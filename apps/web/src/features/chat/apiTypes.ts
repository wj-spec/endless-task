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

/** P0 文件面板：工作区文件树单层条目（relativePath 相对工作区根）。 */
export type WorkspaceTreeEntry = {
  name: string;
  relativePath: string;
  kind: "directory" | "file";
  size: number;
  isHidden: boolean;
};

export type WorkspaceTreeListing = {
  workspaceId: string;
  path: string;
  currentPath: string;
  rootPath: string;
  entries: WorkspaceTreeEntry[];
  truncated: boolean;
};

/** S4 内嵌终端：PTY 会话快照。 */
export type TerminalStatus = {
  kind: "running" | "exited";
  exitCode: number | null;
  signal: string | null;
};

export type TerminalSnapshot = {
  sessionId: string;
  workspaceId: string;
  pid: number;
  cwd: string;
  name: string;
  createdAt: number;
  status: TerminalStatus;
  rows: number;
  cols: number;
};

/** P1 编辑：读取单个工作区文件（含乐观并发版本 token）。 */
export type WorkspaceFileContent = {
  workspaceId: string;
  path: string;
  version: string;
  size: number;
  totalLines: number;
  content: string;
};

export type WorkspaceFileWriteResult = {
  workspaceId: string;
  path: string;
  version: string;
  size: number;
  totalLines: number;
  undoEntryId: string | null;
};

/** 409 冲突时服务端附带的当前状态（用于"重新加载 / 覆盖 / 另存副本"）。 */
export type WorkspaceFileConflictDetails = {
  path?: string;
  currentVersion?: string | null;
  currentContent?: string | null;
  beforeExists?: boolean;
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
  version?: string;
  digest?: string;
  whenToUse?: string;
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
    /** 当前占用（最近一轮请求的上下文大小）。 */
    usedTokens: number;
    usedRatio: number;
    remainingTokens: number;
    /** 本 run 所有轮次累计消耗（成本参考）。 */
    cumulativeTokens?: number;
  };
  interruptedRuns: RuntimeV2RecoveryReport[];
  stuck?: RuntimeV2StuckState | null;
  escalation?: RuntimeV2Escalation | null;
  verification?: RuntimeV2Verification | null;
  usage?: RuntimeV2UsageSummary | null;
  capabilities: string[];
};

/** C5 成本/延迟可见：当前 run 的用量与成本估算。 */
export type RuntimeV2UsageSummary = {
  runId: string;
  turns: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  /** 估算成本（USD）；null = 模型未定价（不给假估算）。 */
  costUsd: number | null;
  /** 所有轮次都已定价时为 true。 */
  costPriced: boolean;
  unpricedTurns: number;
  priceRevision: string | null;
  models: string[];
  durationMs: number | null;
  firstTokenLatencyMs: number | null;
};

/** C1 制造者—检查者分离：独立验证结论。 */
export type RuntimeV2Verification = {
  runId: string;
  /** verifying | verified */
  status: string;
  /** pass | fail | uncertain（verifying 时为 null） */
  verdict: string | null;
  reasons: string[];
  missing: string[];
  model: string;
  latencyMs?: number | null;
  inputTokens?: number | null;
  outputTokens?: number | null;
};

/** C4 终止与升级：无进展/预算将尽时提请人工决策的报告。 */
export type RuntimeV2Escalation = {
  runId: string;
  /** no_progress | budget_exhausted */
  reason: string;
  summary: string;
  /** continue | change_approach | take_over */
  options: string[];
  progress: {
    modelTurns?: number;
    toolCalls?: number;
    toolFailures?: number;
    producedCharacters?: number;
    inputTokens?: number;
    outputTokens?: number;
  };
  budget: {
    usedTokens?: number;
    limitTokens?: number | null;
    usedRatio?: number | null;
  };
  repeatedFailures: Array<{
    toolName: string;
    errorCode: string;
    count: number;
    safeMessage?: string;
  }>;
  failures: Array<{
    toolName: string;
    errorCode: string;
    safeMessage?: string;
    attempt: number;
    toolExecutionId?: string | null;
  }>;
  guidance: string;
  /** 安全停止策略是否已经/将要结束本次运行。 */
  willStop: boolean;
  /** C5：成本超限时的成本信息。 */
  cost?: {
    usedUsd?: number | null;
    capUsd?: number | null;
    priced?: boolean;
  } | null;
  /** C1：验证不通过时的验证结论。 */
  verdict?: {
    verdict: string;
    reasons: string[];
    missing: string[];
    model?: string;
  } | null;
};

/** C2 失败记忆：运行期"卡住"状态（快照 + 实时事件都能给出）。 */
export type RuntimeV2StuckState = {
  runId: string;
  /** remind（刚出现重复失败）| restrict（连续失败升级）| no-progress 级别。 */
  level: string;
  detector: string;
  reasons: string[];
  consecutive?: number | null;
  repeatedFailures: Array<{
    toolName: string;
    errorCode: string;
    count: number;
    safeMessage?: string;
  }>;
  attempts: Array<{
    toolName: string;
    errorCode: string;
    safeMessage?: string;
    attempt: number;
    retryable?: boolean;
    toolExecutionId?: string | null;
  }>;
  guidance: string;
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
  /** S1：显式技能调用结果（ok / disabled / not_user_invocable / …）。 */
  requestedSkills?: { name: string; status: string; message: string }[];
};

/** M2：MCP 调用记录。 */
export type McpCallRecord = {
  time: string;
  operation: string;
  detail: string;
  durationMs: number;
  status: string;
};

/** S2：技能校验结果（清单 + 诊断 + 扫描）。 */
export type SkillValidation = {
  valid: boolean;
  manifest: {
    name: string;
    description: string;
    version: string;
    schemaVersion: number;
    digest: string;
    modelInvocable: boolean;
    userInvocable: boolean;
    requiredTools: string[];
    requiredCapabilities: string[];
    conflictsWith: string[];
  };
  diagnostics: { code: string; message: string; path: string }[];
  scan: {
    worstLevel: string | null;
    quarantined: boolean;
    findings: { code: string; level: string; message: string; path: string }[];
  };
};

/** S2：技能使用统计（按 digest 分代）。 */
export type SkillUsageRow = {
  scope: string;
  name: string;
  digest: string;
  counts: Record<string, number>;
  lastAt: string | null;
};

/** S2：技能包用例结果。 */
export type SkillCaseResult = {
  name: string;
  passed: boolean;
  failures: string[];
};

export type SkillImportResult = {
  name: string;
  version: string;
  digest: string;
  target: string;
  upgraded: boolean;
  replacedDigest: string | null;
  worstLevel: string | null;
};

/** S1：composer `/` 候选（用户可显式调用的技能）。 */
export type SkillInvocationCandidate = {
  name: string;
  description: string;
  scope: "user" | "workspace";
  whenToUse?: string;
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
  /** B3：重要性 0~1（越高越不容易被遗忘）。 */
  importance: number;
  /** B3：被注入使用的次数（间隔重复）。 */
  accessCount: number;
  lastAccessedAt: string | null;
  /** B3：钉住的记忆永不自动遗忘。 */
  pinned: boolean;
};

/** B5 用户画像：注入用的稳定前缀块。 */
export type UserProfileBlock = {
  content: string;
  version: number;
  signature: string;
  lines: string[];
  manual: boolean;
  characters: number;
  refreshed?: boolean;
  versionChanged?: boolean;
  reason?: string;
};

/** B4 反思：一条从失败中归纳出的洞见及其来源。 */
export type MemoryReflectionRecord = {
  id: string;
  conversationId: string;
  runId: string | null;
  trigger: string;
  /** pending | accepted | rejected */
  status: string;
  insight: string;
  proposalId: string;
  insightMemoryId: string | null;
  createdAt: string;
  resolvedAt: string | null;
  sources: Array<Record<string, unknown>>;
};

/** A8 决策解释/审计轨迹：一条带"为什么"的运行事实。 */
export type AuditTrailEntry = {
  id: string;
  kind: string;
  title: string;
  summary: string;
  rationale: string;
  counterfactual: string;
  uncertainty: string;
  /** info | warning | critical */
  severity: string;
  occurredAt: string;
  toolName: string;
  effect: string;
  risk: string;
  decision: string;
  options: string[];
  refs: Record<string, string>;
};

export type AuditTrailResponse = {
  runId: string;
  conversationId: string;
  items: AuditTrailEntry[];
  counts: Record<string, number>;
};

/** A5 撤销/回滚：一条可逆的工具副作用记录。 */
export type UndoJournalEntry = {
  id: string;
  conversationId: string;
  workspaceId: string | null;
  runId: string | null;
  /** file_write | file_delete */
  kind: string;
  target: string;
  description: string;
  /** available | undone */
  status: string;
  undoable: boolean;
  createdAt: string;
  undoneAt: string | null;
};

/** B2：一次记忆巩固记录（聚类 → 提案 → 并入）。 */
export type MemoryConsolidationRecord = {
  id: string;
  proposalId: string;
  kind: string;
  status: "pending" | "accepted" | "rejected";
  signature: string;
  insightMemoryId: string | null;
  createdAt: string;
  resolvedAt: string | null;
  sources: Array<{ id: string; content: string; status: string }>;
};

/** B2：一次巩固扫描的结果。 */
export type MemoryConsolidationRunReport = {
  clusterCount: number;
  createdCount: number;
  created: Array<{
    kind: string;
    memoryIds: string[];
    contents: string[];
    signature: string;
    mergedContent: string;
    conversationId: string;
    turnId: string;
    proposalId: string;
    importance: number;
    reason: string;
  }>;
  skipped: string[];
};

/** B3：一次遗忘巡检的结果。 */
export type MemoryForgettingReport = {
  dryRun: boolean;
  forgottenCount: number;
  forgotten: Array<{
    memoryId: string;
    content: string;
    importance: number;
    accessCount: number;
    pinned: boolean;
  }>;
  needsReview: Array<{
    memoryId: string;
    content: string;
    importance: number;
    accessCount: number;
    pinned: boolean;
    probability: number;
    needsReview?: boolean;
  }>;
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
