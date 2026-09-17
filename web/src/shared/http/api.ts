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
  ResumeDraft,
  ResumeDraftData,
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
  activeLogin: () => request<LoginFlowResponse>("/login-flows/active"),
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
  resumeDrafts: (accountId: number) => request<ResumeDraft[]>(`/accounts/${accountId}/resume-drafts`),
  resumeDraft: (accountId: number, draftId: number) => request<ResumeDraft>(`/accounts/${accountId}/resume-drafts/${draftId}`),
  createResumeDraft: (accountId: number, source: "MANUAL" | "PDF") =>
    request<ResumeDraft>(`/accounts/${accountId}/resume-drafts`, { method: "POST", body: JSON.stringify({ source }) }),
  patchResumeDraft: (accountId: number, draftId: number, expectedRevision: number, currentStep: string, data: ResumeDraftData) =>
    request<ResumeDraft>(`/accounts/${accountId}/resume-drafts/${draftId}`, {
      method: "PATCH", body: JSON.stringify({ expected_revision: expectedRevision, current_step: currentStep, data }),
    }),
  deleteResumeDraft: (accountId: number, draftId: number) =>
    request<void>(`/accounts/${accountId}/resume-drafts/${draftId}`, { method: "DELETE" }),
  extractResumePdf: (accountId: number, draftId: number, file: File) => {
    const body = new FormData();
    body.set("file", file);
    return request<OperationStart>(`/accounts/${accountId}/resume-drafts/${draftId}/extract-pdf`, { method: "POST", body });
  },
  validateResumeDraft: (accountId: number, draftId: number) =>
    request<{ status: string; valid: boolean; field_errors: import("../types/api").ResumeFieldError[] }>(`/accounts/${accountId}/resume-drafts/${draftId}/validate`, { method: "POST" }),
  preflightResumeDraft: (accountId: number, draftId: number) =>
    request<OperationStart>(`/accounts/${accountId}/resume-drafts/${draftId}/preflight`, { method: "POST" }),
  publishResumeDraft: (accountId: number, draftId: number, expectedRevision: number, idempotencyKey: string, confirmationFingerprint: string) =>
    request<OperationStart>(`/accounts/${accountId}/resume-drafts/${draftId}/publish`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision, idempotency_key: idempotencyKey, confirmation_fingerprint: confirmationFingerprint }),
    }),
  resumeResumeDraft: (accountId: number, draftId: number, expectedRevision: number, confirmationFingerprint: string) =>
    request<OperationStart>(`/accounts/${accountId}/resume-drafts/${draftId}/resume`, {
      method: "POST",
      body: JSON.stringify({ expected_revision: expectedRevision, confirmation_fingerprint: confirmationFingerprint }),
    }),
  reconcileResumeDraft: (accountId: number, draftId: number) =>
    request<OperationStart>(`/accounts/${accountId}/resume-drafts/${draftId}/reconcile`, { method: "POST" }),

  questionnaires: async (accountId?: number) => {
    const result: Questionnaire[] = [];
    let beforeId: number | undefined;
    for (;;) {
      const params = new URLSearchParams({ limit: "100" });
      if (accountId) params.set("account_id", String(accountId));
      if (beforeId) params.set("before_id", String(beforeId));
      const page = await request<Questionnaire[]>(`/questionnaires?${params}`);
      result.push(...page);
      if (page.length < 100) return result;
      beforeId = page[page.length - 1].id;
    }
  },
  questionnaire: (applyId: number) => request<Questionnaire>(`/questionnaires/${applyId}`),
  updateQuestionnaire: (id: number, values: { expected_revision: number; cover_letter?: string; answers?: Questionnaire["ai_payload"]["answers"] }) =>
    request<Questionnaire>(`/questionnaires/${id}`, { method: "PATCH", body: JSON.stringify(values) }),
  confirmQuestionnaire: (id: number, expectedRevision: number) => request<{ status: string; apply_id: number }>(`/questionnaires/${id}/confirm`, {
    method: "POST", body: JSON.stringify({ expected_revision: expectedRevision }),
  }),
  skipQuestionnaire: (id: number) => request<Questionnaire>(`/questionnaires/${id}/skip`, { method: "POST" }),
  applications: (accountId?: number, beforeId?: number, limit = 50) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (accountId) params.set("account_id", String(accountId));
    if (beforeId) params.set("before_id", String(beforeId));
    return request<{ history: Dashboard["recent_events"]; review_required: Dashboard["recent_events"]; search_runs: Array<{ id: number; status: string; processed: number; found: number; details: string; created_at: string }>; stats: Dashboard["stats"] }>(`/applications?${params}`);
  },
  resolveApplication: (attemptId: string, applied: boolean) => request<{ attempt_id: string; status: string; resolved: boolean; changed: boolean }>(`/applications/${attemptId}/resolve`, {
    method: "POST", body: JSON.stringify({ applied }),
  }),
  exportApplicationHistory: () => request<Record<string, unknown>>("/applications/export"),
  deleteApplicationHistory: () => request<Record<string, number>>("/applications/history", { method: "DELETE" }),

  audits: async (filters: { accountId?: number; independentOnly?: boolean } = {}) => {
    const result: Audit[] = [];
    let beforeId: number | undefined;
    for (;;) {
      const params = new URLSearchParams({ limit: "100" });
      if (filters.accountId) params.set("account_id", String(filters.accountId));
      if (filters.independentOnly) params.set("independent_only", "true");
      if (beforeId) params.set("before_id", String(beforeId));
      const page = await request<Audit[]>(`/audits?${params}`);
      result.push(...page);
      if (page.length < 100) return result;
      beforeId = page[page.length - 1].id;
    }
  },
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
  operations: (filters: { kind?: string; accountId?: number; resource?: string; activeOnly?: boolean } = {}) => {
    const params = new URLSearchParams();
    if (filters.kind) params.set("kind", filters.kind);
    if (filters.accountId) params.set("account_id", String(filters.accountId));
    if (filters.resource) params.set("resource", filters.resource);
    params.set("active_only", String(filters.activeOnly ?? true));
    return request<Operation[]>(`/operations?${params}`);
  },
};
