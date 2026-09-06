import { ApiClientError } from "./api";

/** User-facing readable text for one local-API failure (pure). */
export const readableError = (error: unknown): string => {
  if (error instanceof ApiClientError) return error.message;
  if (error instanceof Error && error.name === "AbortError") return "";
  return "无法连接本地服务，请确认 API 已启动。";
};
