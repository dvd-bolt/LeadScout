import { telegram } from "../telegram/sdk";

let csrfToken = "";

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
    this.name = "ApiError";
  }
}

export function setCsrfToken(value: string) {
  csrfToken = value;
}

export async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (csrfToken && options.method && options.method !== "GET") headers.set("X-CSRF-Token", csrfToken);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`/api/v1${path}`, { ...options, headers, credentials: "same-origin" });
  if (!response.ok) {
    const error = (await response.json().catch(() => ({ detail: "Не удалось выполнить запрос" }))) as { detail?: unknown };
    const detail = typeof error.detail === "string" ? error.detail : Array.isArray(error.detail)
      ? error.detail.map((item: { loc?: string[]; msg?: string }) => `${item.loc?.slice(1).join(".") || "Поле"}: ${item.msg || "некорректное значение"}`).join("; ")
      : "Не удалось выполнить запрос";
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export async function authenticate() {
  const initData = telegram()?.initData;
  if (!initData) throw new Error("Откройте LeadScout из Telegram.");
  const payload = await request<{ csrf_token: string }>("/auth/telegram", {
    method: "POST",
    body: JSON.stringify({ init_data: initData }),
  });
  setCsrfToken(payload.csrf_token);
}
