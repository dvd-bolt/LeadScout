export type Account = {
  id: number;
  account_name: string;
  session_status: string;
  active_resume_hh_id: string;
  active_resume_title: string;
  daily_limit: number;
  applied_today: number;
  applied_date: string;
  auto_apply_enabled: boolean;
  automation_state: string;
  only_remote: boolean;
  send_cover_letter: boolean;
  min_salary: number;
  keywords: string;
  stop_words: string;
  has_proxy: boolean;
  last_synced_at: string;
  next_scheduled_search_at: string;
  resume_ready: boolean;
  pending_captcha_data_uri?: string;
  pending_captcha_created_at?: string;
};

export type ActiveCaptcha = {
  account_id: number;
  captcha_data_uri: string;
  page_url?: string;
  created_at?: string;
};

export type CaptchaSubmitResult = {
  status: "SUCCESS" | "INVALID_CAPTCHA" | "ERROR" | string;
  account_id: number;
  captcha_data_uri?: string;
  message?: string;
  expires_in?: number;
};

export type Event = {
  id: number;
  account_id: number;
  vacancy_hh_id: string;
  vacancy_title: string;
  company: string;
  status: string;
  details: string;
  attempt_id?: string;
  stage?: string;
  created_at: string;
  resolved_at?: string;
  cover_letter?: string;
  resume_snapshot_id?: number | null;
  resume_hh_id?: string;
  resume_title?: string;
};

export type Dashboard = {
  role: "ROOT" | "ADMIN" | "USER";
  admin_capabilities: string[];
  user_id: number;
  csrf_token: string;
  accounts: Account[];
  active_account_id: number | null;
  active_captcha?: ActiveCaptcha | null;
  stats: { applied: number; processed: number; errors: number; skipped: number };
  pending_review_count: number;
  active_pending_review_count: number;
  recent_events: Event[];
  next_scheduled_search_at: string;
};

export type Resume = {
  snapshot_id: number;
  id: string;
  hh_resume_id: string;
  title: string;
  href: string;
  status: string;
  extracted_text: string;
  synced_at: string;
};

export type QuestionnaireAnswer = { field_id: string; value: string | string[]; answer_type: string };

export type Questionnaire = {
  revision: number;
  id: number;
  account_id: number;
  vacancy_url: string;
  vacancy_title: string;
  cover_letter: string;
  questions: Array<{ field_id: string; label: string; answer_type: string; required: boolean; options: string[] }>;
  ai_payload: { answers?: QuestionnaireAnswer[] };
  resume_title: string;
  status: string;
  error_text: string;
  updated_at: string;
};

export type AuditInsight = {
  tier: number;
  title: string;
  description: string;
  score_impact: string;
};

export type Audit = {
  id: number;
  account_id?: number | null;
  source_account_name?: string;
  profession_name: string;
  overall_score: number;
  category_scores: Record<string, number>;
  penalties: string[];
  top_recommendations: string[];
  insights: AuditInsight[];
  summary_text: string;
  created_at: string;
};

export type StructuredResume = Record<string, unknown> & {
  first_name?: string;
  birth_date?: string;
  city?: string;
  title?: string;
};

export type ResumeDraftData = {
  profession: { title: string; hh_profession: string; hh_profession_id: string; specializations: string[] };
  personal: {
    first_name: string; last_name: string; middle_name: string; birth_date: string; gender: string; city: string; hh_city_id: string;
    citizenships: string[]; work_authorizations: string[];
  };
  contacts: { phone: string; email: string; telegram: string; preferred: string; methods: string[] };
  work_conditions: {
    salary: number | null; currency: string; employment_types: string[]; schedules: string[];
    work_formats: string[]; relocation: string; business_trips: string;
  };
  skills: Array<{ name: string; level: string }>;
  experiences: Array<{
    company: string; position: string; city: string; start_month: string; start_year: string;
    is_current: boolean; end_month: string; end_year: string; description: string; selected: boolean;
  }>;
  education: Array<{
    level: string; institution: string; faculty: string; specialization: string; end_year: string; selected: boolean;
  }>;
  languages: Array<{ name: string; level: string }>;
  additional: {
    courses: NamedResumeDetail[]; exams: NamedResumeDetail[]; certificates: NamedResumeDetail[];
    recommendations: NamedResumeDetail[]; driving_licenses: string[]; has_car: boolean;
  };
  about: { text: string; links: Array<{ label: string; url: string }> };
  publication: { visibility: string; target_account_confirmed: boolean };
};

export type NamedResumeDetail = { name: string; organization: string; year: string; description: string };

export type ResumeFieldError = { path: string; code: string; message: string };

export type ResumeParseError = {
  code: string;
  stage: "EXTRACT" | "PARSE" | string;
  message: string;
  retryable: boolean;
  required_action: string;
};

export type ResumeExtractionSummary = {
  source_sections: Partial<Record<"experiences" | "education" | "skills" | "about", boolean>>;
  extracted_counts: Partial<Record<"experiences" | "education" | "skills" | "about", number>>;
  current_counts: Partial<Record<"experiences" | "education" | "skills" | "about", number>>;
  inferred_birth_date_removed: boolean;
};

export type ResumeDraft = {
  id: number;
  account_id: number;
  source: "MANUAL" | "PDF";
  schema_version: number;
  revision: number;
  current_step: string;
  status: "DRAFT" | "PARSING" | "READY" | "PUBLISHING" | "COMPLETED" | "NEEDS_INPUT" | "NEEDS_REVIEW" | "FAILED";
  data: ResumeDraftData;
  validation: {
    valid?: boolean;
    field_errors?: ResumeFieldError[];
    parse_error?: ResumeParseError;
    extraction_summary?: ResumeExtractionSummary;
    extraction_warnings?: ResumeFieldError[];
  };
  preflight: {
    conflicts?: Array<{ path: string; draft_value: string; profile_value: string }>;
    profile_changes?: Array<{ path: string; draft_value: string; profile_value: string }>;
    capabilities?: { max_skills?: number; supports_custom_skills?: boolean };
  };
  preflight_revision: number | null;
  preflight_fingerprint: string;
  hh_resume_id: string;
  hh_resume_url: string;
  hh_status: string;
  latest_publish_attempt_id?: string | null;
  latest_publish_status?: string | null;
  latest_publish_stage?: string | null;
  updated_at: string;
};

export type NeedsFieldsResult = {
  status: "NEEDS_FIELDS";
  missing_fields: string[];
  structured: StructuredResume;
  message?: string;
};

export type OperationResultStatus =
  | "SUCCESS" | "SUCCEEDED" | "STARTED" | "ALREADY_RUNNING" | "READY" | "CANCELLED" | "STOPPED"
  | "NEEDS_FIELDS" | "NEEDS_INPUT" | "NEEDS_REVIEW" | "NEEDS_ACTION" | "PARTIAL" | "UNCERTAIN"
  | "WAITING_FOR_CAPTCHA" | "WAITING_FOR_OTP" | "WAITING_FOR_CODE" | "WAITING_FOR_SMS"
  | "INVALID_CAPTCHA" | "INVALID_CODE" | "PENDING" | "SUBMITTED" | "SKIPPED"
  | "SKIPPED_LIMIT" | "SKIPPED_NOT_AUTHORIZED" | "SKIPPED_NO_RESUME" | "SKIPPED_NO_SESSION"
  | "SKIPPED_STOPPED" | "CLIENT_UPDATE_REQUIRED" | "DAILY_LIMIT" | "ERROR" | "ERROR_CAPTCHA"
  | "ERROR_INVALID_URL" | "ERROR_SESSION_EXPIRED" | "EXPIRED_SESSION" | "FAILED"
  | "MISSING_RESUME" | "NOT_FOUND" | "NO_SESSION";

export type Operation = {
  id: string;
  kind: string;
  account_id?: number | null;
  resource?: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "NEEDS_INPUT";
  result: (Record<string, unknown> & {
    status?: OperationResultStatus; code?: string; stage?: string; message?: string;
    retryable?: boolean; required_action?: string; draft_id?: number;
  }) | NeedsFieldsResult;
  error_text: string;
};

export type OperationStart = { operation_id: string; status?: string; attempt_id?: string; reused?: boolean };
export type AutomationStartResult = { account_id: number; status: string; message?: string };
export type LoginFlowStatus =
  | "STARTING"
  | "WAITING_FOR_CAPTCHA"
  | "WAITING_FOR_OTP"
  | "SUCCESS"
  | "INVALID_CAPTCHA"
  | "INVALID_CODE"
  | "ERROR";

export type LoginFlowResponse = {
  account_id?: number;
  status: LoginFlowStatus | string;
  code?: string;
  captcha_data_uri?: string;
  message?: string;
  expires_in?: number;
};
export type VacancyMatchInput = { vacancy_text: string } | { vacancy_url: string };
