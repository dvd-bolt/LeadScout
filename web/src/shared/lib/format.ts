import type { Account } from "../types/api";

export function formatDate(value?: string) {
  if (!value) return "—";
  const normalized = value.replace(" ", "T");
  const date = new Date(normalized + (/[zZ]|[+-]\d\d:\d\d$/.test(normalized) ? "" : "Z"));
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short", timeZone: "Europe/Moscow" }).format(date);
}

export function accountStatusLabel(account: Account) {
  if (account.automation_state === "WAITING_FOR_CAPTCHA" || account.pending_captcha_data_uri) {
    return { text: "Требуется ввод капчи", tone: "warning" as const };
  }
  if (account.automation_state === "SEARCHING") return { text: "Идёт поиск", tone: "good" as const };
  if (account.automation_state === "ERROR") return { text: "Ошибка", tone: "danger" as const };
  if (account.session_status !== "ACTIVE") return { text: "Нужен вход", tone: "warning" as const };
  if (!account.resume_ready) return { text: "Выберите резюме", tone: "warning" as const };
  if (account.applied_today >= account.daily_limit) return { text: "Лимит исчерпан", tone: "warning" as const };
  if (!account.auto_apply_enabled) return { text: "Готов к запуску", tone: "neutral" as const };
  switch (account.automation_state) {
    case "SEARCHING": return { text: "Идёт поиск", tone: "good" as const };
    case "QUEUED": return { text: "В очереди · автоматизация включена", tone: "good" as const };
    case "NEEDS_LOGIN": return { text: "Нужен вход", tone: "warning" as const };
    case "ERROR": return { text: "Ошибка", tone: "danger" as const };
    case "WAITING": return { text: "Ожидается следующий запуск", tone: "neutral" as const };
    default: return { text: `Состояние: ${account.automation_state || "неизвестно"}`, tone: "neutral" as const };
  }
}

export function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "Не удалось выполнить запрос";
}


export function formatMoscowTime(value?: string) {
  if (!value) return "—";
  const normalized = value.replace(" ", "T");
  const date = new Date(normalized + (/[zZ]|[+-]\d\d:\d\d$/.test(normalized) ? "" : "Z"));
  return Number.isNaN(date.valueOf()) ? "—" : new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/Moscow" }).format(date);
}

export function reviewCountLabel(count: number) {
  const plural = new Intl.PluralRules("ru").select(count);
  return `${count} ${plural === "one" ? "анкета ждёт" : plural === "few" ? "анкеты ждут" : "анкет ждут"} проверки`;
}

export function eventStatusLabel(status: string) {
  if (status === "ALREADY_APPLIED") return "Откликались ранее";
  if (status.startsWith("APPLIED")) return "Отклик отправлен";
  if (status.startsWith("ERROR")) return "Ошибка отклика";
  if (status.startsWith("SKIPPED")) return "Вакансия пропущена";
  if (status === "QUESTIONNAIRE_REQUIRED") return "Нужны ответы";
  if (status === "REVIEWED_NOT_APPLIED") return "Проверено: отклик не отправлен";
  return status;
}

export function applicationStageLabel(stage?: string) {
  switch (stage) {
    case "SEARCH": return "поиск";
    case "LOADING": return "загрузка вакансии";
    case "PARSING": return "разбор вакансии";
    case "AI_PREPARATION": return "подготовка ИИ";
    case "FILLING": return "заполнение формы";
    case "SUBMITTING": return "отправка формы";
    case "CONFIRMING": return "подтверждение результата";
    default: return stage || "";
  }
}

export function resumeStatusLabel(status: string) {
  return ({
    DRAFT: "Черновик", PARSING: "Распознаётся", READY: "Готов к проверке",
    PUBLISHING: "Публикуется", COMPLETED: "Опубликован", NEEDS_INPUT: "Нужно заполнить",
    NEEDS_REVIEW: "Нужна проверка", FAILED: "Ошибка",
  } as Record<string, string>)[status] || "Неизвестное состояние";
}

export function resumeStepLabel(step: string) {
  return ({
    profession: "Профессия", personal: "Личные данные", contacts: "Контакты",
    conditions: "Условия", skills: "Навыки", experience: "Опыт", education: "Образование",
    languages: "Языки", additional: "Дополнительно", about: "О себе", review: "Проверка",
  } as Record<string, string>)[step] || "Проверка";
}

export function auditCategoryLabel(category: string) {
  return ({
    hard_skills: "Профессиональные навыки", impact_metrics: "Результаты и метрики",
    parseability: "Читаемость", timeline: "Хронология", style: "Стиль",
  } as Record<string, string>)[category] || category.replaceAll("_", " ");
}
