import type {
  Account,
  Audit,
  AutomationStartResult,
  Dashboard,
  LoginFlowResponse,
  Operation,
  OperationStart,
  Questionnaire,
  Resume,
  StructuredResume,
  VacancyMatchInput,
} from "../types/api";
import { request } from "./client";

export const api = {
  dashboard: () => request<Dashboard>("/me"),
  accounts: () => request<Account[]>("/accounts"),
  activateAccount: (accountId: number) => request<{ active_account_id: number }>(`/accounts/${accountId}/activate`, { method: "POST" }),
  deleteAccount: (accountId: number) => request<void>(`/accounts/${accountId}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) }),
  patchAccount: (accountId: number, values: Partial<Account> & { proxy_url?: string }) =>
    request<Account>(`/accounts/${accountId}`, { method: "PATCH", body: JSON.stringify(values) }),

  startAutomation: (accountId: number) => request<AutomationStartResult>(`/automation/${accountId}/start`, { method: "POST" }),
  startAll: () => request<{ results: AutomationStartResult[] }>("/automation/start-all", { method: "POST" }),
  stopAutomation: (accountId: number) => request<AutomationStartResult>(`/automation/${accountId}/stop`, { method: "POST" }),
  stopAll: () => request<{ status: string; account_ids: number[] }>("/automation/stop-all", { method: "POST" }),

  startLogin: (phone_or_email: string, account_name = "") =>
    request<LoginFlowResponse>("/login-flows/start", { method: "POST", body: JSON.stringify({ phone_or_email, account_name }) }),
  submitOtp: (code: string, accountId?: number) => request<LoginFlowResponse>("/login-flows/otp", { method: "POST", body: JSON.stringify({ code, account_id: accountId }) }),
  submitCaptcha: (code: string, accountId?: number) => request<LoginFlowResponse>("/login-flows/captcha", { method: "POST", body: JSON.stringify({ code, account_id: accountId }) }),
  reloadCaptcha: (accountId?: number) => request<LoginFlowResponse>("/login-flows/captcha/reload", { method: "POST", body: JSON.stringify({ account_id: accountId }) }),
  switchCaptchaLanguage: (accountId?: number) => request<LoginFlowResponse>("/login-flows/captcha/language", { method: "POST", body: JSON.stringify({ account_id: accountId }) }),
  cancelLogin: (accountId?: number) => request<void>("/login-flows/cancel", { method: "POST", body: JSON.stringify({ account_id: accountId }) }),

  submitAutomationCaptcha: (accountId: number, code: string) =>
    request<import("../types/api").CaptchaSubmitResult>("/captcha/submit", { method: "POST", body: JSON.stringify({ account_id: accountId, code }) }),
  reloadAutomationCaptcha: (accountId: number) =>
    request<import("../types/api").CaptchaSubmitResult>("/captcha/reload", { method: "POST", body: JSON.stringify({ account_id: accountId }) }),

  resumes: (accountId: number) => request<Resume[]>(`/accounts/${accountId}/resumes`),
  syncResumes: (accountId: number) => request<OperationStart>(`/accounts/${accountId}/resumes/sync`, { method: "POST" }),
  activateResume: (accountId: number, snapshotId: number) => request<Resume>(`/accounts/${accountId}/resumes/${snapshotId}/activate`, { method: "POST" }),
  deleteResume: (accountId: number, snapshotId: number) =>
    request<OperationStart>(`/accounts/${accountId}/resumes/${snapshotId}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) }),
  importResume: (accountId: number, file: File, structured?: StructuredResume) => {
    const body = new FormData();
    body.set("file", file);
    if (structured) body.set("structured_json", JSON.stringify(structured));
    return request<OperationStart>(`/accounts/${accountId}/resumes/import`, { method: "POST", body });
  },

  questionnaires: (accountId?: number) => request<Questionnaire[]>(`/questionnaires${accountId ? `?account_id=${accountId}` : ""}`),
  questionnaire: (applyId: number) => request<Questionnaire>(`/questionnaires/${applyId}`),
  updateQuestionnaire: (id: number, values: { cover_letter?: string; answers?: Questionnaire["ai_payload"]["answers"] }) =>
    request<Questionnaire>(`/questionnaires/${id}`, { method: "PATCH", body: JSON.stringify(values) }),
  confirmQuestionnaire: (id: number, expectedRevision: number) => request<{ status: string; apply_id: number }>(`/questionnaires/${id}/confirm`, {
    method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }),
  }),
  skipQuestionnaire: (id: number) => request<Questionnaire>(`/questionnaires/${id}/skip`, { method: "POST" }),
  applications: (accountId?: number) => request<{ history: Dashboard["recent_events"]; stats: Dashboard["stats"] }>(`/applications${accountId ? `?account_id=${accountId}` : ""}`),
  resolveApplication: (attemptId: string, applied: boolean) => request<{ attempt_id: string; status: string; resolved: boolean; changed: boolean }>(`/applications/${attemptId}/resolve`, {
    method: "POST", body: JSON.stringify({ applied }),
  }),

  audits: (accountId?: number) => request<Audit[]>(`/audits${accountId ? `?account_id=${accountId}` : ""}`),
  createAudit: (values: { account_id?: number; resume_snapshot_id?: number; resume_text?: string }) =>
    request<OperationStart>("/audits", { method: "POST", body: JSON.stringify(values) }),
  createPdfAudit: (accountId: number | undefined, file: File) => {
    const body = new FormData();
    body.set("file", file);
    if (accountId !== undefined) body.set("account_id", String(accountId));
    return request<OperationStart>("/audits/pdf", { method: "POST", body });
  },
  matchAudit: (auditId: number, vacancy: VacancyMatchInput) =>
    request<OperationStart>(`/audits/${auditId}/match`, { method: "POST", body: JSON.stringify(vacancy) }),
  operation: (id: string) => request<Operation>(`/operations/${id}`),
};
