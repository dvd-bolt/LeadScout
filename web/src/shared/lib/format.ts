import type { Account } from "../types/api";

export function formatDate(value?: string) {
  if (!value) return "—";
  const normalized = value.replace(" ", "T");
  const date = new Date(normalized + (/[zZ]|[+-]\d\d:\d\d$/.test(normalized) ? "" : "Z"));
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short", timeZone: "Europe/Moscow" }).format(date);
}

export function accountStatusLabel(account: Account) {
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
  if (status.startsWith("APPLIED")) return "Отклик отправлен";
  if (status.startsWith("ERROR")) return "Ошибка отклика";
  if (status.startsWith("SKIPPED")) return "Вакансия пропущена";
  if (status === "QUESTIONNAIRE_REQUIRED") return "Нужны ответы";
  if (status === "ALREADY_APPLIED") return "Отклик уже отправлен";
  return status;
}
