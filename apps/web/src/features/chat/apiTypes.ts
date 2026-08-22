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
};

export type ConversationSnapshot = {
  conversation: Conversation;
  turns: ConversationTurnSnapshot[];
};

export type RuntimeErrorDetail = {
  code: string;
  message: string;
  retryable: boolean;
  retryAfterMs?: number;
  correlationId: string;
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
};
