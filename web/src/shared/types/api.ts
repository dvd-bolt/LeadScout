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
};

export type Event = {
  id: number;
  account_id: number;
  vacancy_hh_id: string;
  vacancy_title: string;
  company: string;
  status: string;
  details: string;
  created_at: string;
};

export type Dashboard = {
  user_id: number;
  csrf_token: string;
  accounts: Account[];
  active_account_id: number | null;
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

export type QuestionnaireAnswer = { field_id: string; value: string; answer_type: string };

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

export type NeedsFieldsResult = {
  status: "NEEDS_FIELDS";
  missing_fields: string[];
  structured: StructuredResume;
  message?: string;
};

export type Operation = {
  id: string;
  kind: string;
  status: "PENDING" | "RUNNING" | "SUCCEEDED" | "FAILED" | "NEEDS_INPUT";
  result: Record<string, unknown> | NeedsFieldsResult;
  error_text: string;
};

export type OperationStart = { operation_id: string; status?: string };
export type AutomationStartResult = { account_id: number; status: string; message?: string };
export type LoginFlowResponse = { account_id?: number; status: string; captcha_data_uri?: string; message?: string };
export type VacancyMatchInput = { vacancy_text: string } | { vacancy_url: string };
