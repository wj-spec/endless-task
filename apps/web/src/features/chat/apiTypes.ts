export type ConversationStatus = "active" | "archived";
export type TurnStatus = "created" | "running" | "completed" | "failed" | "cancelled";
export type ResponseVariantStatus = TurnStatus;
export type ResponseVariantOperation = "create" | "retry" | "regenerate";

export type Conversation = {
  id: string;
  title: string;
  status: ConversationStatus;
  nextTurnOrdinal: number;
  titleIsManual: boolean;
  createdAt: string;
  updatedAt: string;
  archivedAt: string | null;
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

export type RuntimeEvent = {
  version: 1;
  eventId: string;
  sequence: number;
  type:
    | "turn.started"
    | "message.started"
    | "message.delta"
    | "message.completed"
    | "approval.requested"
    | "approval.resolved"
    | "activity.started"
    | "activity.completed"
    | "activity.failed"
    | "activity.cancelled"
    | "turn.completed"
    | "turn.failed"
    | "turn.cancelled";
  conversationId: string;
  turnId: string;
  responseVariantId?: string;
  messageId?: string;
  occurredAt: string;
  data: {
    delta?: string;
    content?: string;
    finishReason?: string;
    partialContent?: string;
    error?: RuntimeErrorDetail;
    approvalId?: string;
    toolCallId?: string;
    summary?: string;
    reason?: string;
    status?: ApprovalStatus | ActivityStatus;
    createdAt?: string;
    resolvedAt?: string;
    metadata?: Record<string, unknown>;
    activityId?: string;
    message?: string;
  };
};

export type TurnCommandResponse = {
  conversationId: string;
  turnId: string;
  responseVariantId: string;
  userMessageId?: string;
  assistantMessageId: string;
  eventsUrl: string;
};

export type CompactTurnSnapshot = {
  conversationId: string;
  turnId: string;
  turnStatus: TurnStatus;
  activeResponseVariantId: string;
  responseVariantStatus: ResponseVariantStatus;
  assistantMessageId: string;
  content: string;
  lastSequence: number;
  error?: RuntimeErrorDetail;
  pendingApproval?: ApprovalRequest;
  activities: ActivitySnapshot[];
};

export type HealthSnapshot = {
  status: string;
  provider: string;
  model: string;
  providerConfigured: boolean;
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

export type MemoryRecord = {
  id: string;
  kind: string;
  content: string;
  status: MemoryStatus;
  sourceConversationId: string;
  sourceTurnId: string;
  writeOrigin: string;
  createdAt: string;
  updatedAt: string;
  sourceProposalId: string | null;
  expiredReason: string | null;
  supersededBy: string | null;
};
