import type { Account, Audit, Dashboard, Operation, Questionnaire, Resume } from "./types";
import { telegram } from "./telegram";

let csrfToken = "";

export function setCsrfToken(value: string) {
  csrfToken = value;
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (csrfToken && options.method && options.method !== "GET") headers.set("X-CSRF-Token", csrfToken);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(`/api/v1${path}`, { ...options, headers, credentials: "same-origin" });
  if (!response.ok) {
    const error = (await response.json().catch(() => ({ detail: "Не удалось выполнить запрос" }))) as { detail?: string };
    throw new Error(error.detail || "Не удалось выполнить запрос");
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

export const api = {
  dashboard: () => request<Dashboard>("/me"),
  accounts: () => request<Account[]>("/accounts"),
  activateAccount: (accountId: number) => request(`/accounts/${accountId}/activate`, { method: "POST" }),
  deleteAccount: (accountId: number) => request(`/accounts/${accountId}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) }),
  patchAccount: (accountId: number, values: Partial<Account> & { proxy_url?: string }) =>
    request<Account>(`/accounts/${accountId}`, { method: "PATCH", body: JSON.stringify(values) }),
  startAutomation: (accountId: number) => request(`/automation/${accountId}/start`, { method: "POST" }),
  stopAutomation: (accountId: number) => request(`/automation/${accountId}/stop`, { method: "POST" }),
  stopAll: () => request("/automation/stop-all", { method: "POST" }),
  startLogin: (phone_or_email: string, account_name = "") =>
    request<{ account_id: number; status: string; captcha_data_uri?: string; message?: string }>("/login-flows/start", {
      method: "POST",
      body: JSON.stringify({ phone_or_email, account_name }),
    }),
  submitOtp: (code: string) => request<{ status: string; captcha_data_uri?: string; message?: string }>("/login-flows/otp", { method: "POST", body: JSON.stringify({ code }) }),
  submitCaptcha: (code: string) =>
    request<{ status: string; captcha_data_uri?: string; message?: string }>("/login-flows/captcha", {
      method: "POST",
      body: JSON.stringify({ code }),
    }),
  reloadCaptcha: () => request<{ status: string; captcha_data_uri?: string }>("/login-flows/captcha/reload", { method: "POST" }),
  cancelLogin: () => request<void>("/login-flows/cancel", { method: "POST" }),
  resumes: (accountId: number) => request<Resume[]>(`/accounts/${accountId}/resumes`),
  syncResumes: (accountId: number) => request<{ operation_id: string }>(`/accounts/${accountId}/resumes/sync`, { method: "POST" }),
  activateResume: (accountId: number, snapshotId: number) =>
    request(`/accounts/${accountId}/resumes/${snapshotId}/activate`, { method: "POST" }),
  deleteResume: (accountId: number, snapshotId: number) =>
    request<{ operation_id: string }>(`/accounts/${accountId}/resumes/${snapshotId}`, {
      method: "DELETE",
      body: JSON.stringify({ confirm: true }),
    }),
  importResume: (accountId: number, file: File) => {
    const body = new FormData();
    body.set("file", file);
    return request<{ operation_id: string }>(`/accounts/${accountId}/resumes/import`, { method: "POST", body });
  },
  questionnaires: (accountId?: number) =>
    request<Questionnaire[]>(`/questionnaires${accountId ? `?account_id=${accountId}` : ""}`),
  updateQuestionnaire: (id: number, values: { cover_letter?: string; answers?: unknown[] }) =>
    request<Questionnaire>(`/questionnaires/${id}`, { method: "PATCH", body: JSON.stringify(values) }),
  confirmQuestionnaire: (id: number) => request(`/questionnaires/${id}/confirm`, { method: "POST" }),
  applications: (accountId?: number) =>
    request<{ history: Dashboard["recent_events"]; stats: Dashboard["stats"] }>(
      `/applications${accountId ? `?account_id=${accountId}` : ""}`,
    ),
  audits: (accountId?: number) => request<Audit[]>(`/audits${accountId ? `?account_id=${accountId}` : ""}`),
  createAudit: (values: { account_id: number; resume_snapshot_id?: number; resume_text?: string }) =>
    request<{ operation_id: string }>("/audits", { method: "POST", body: JSON.stringify(values) }),
  createPdfAudit: (accountId: number, file: File) => {
    const body = new FormData();
    body.set("file", file);
    body.set("account_id", String(accountId));
    return request<{ operation_id: string }>("/audits/pdf", { method: "POST", body });
  },
  matchAudit: (auditId: number, vacancyText: string) =>
    request<{ operation_id: string }>(`/audits/${auditId}/match`, { method: "POST", body: JSON.stringify({ vacancy_text: vacancyText }) }),
  operation: (id: string) => request<Operation>(`/operations/${id}`),
};
