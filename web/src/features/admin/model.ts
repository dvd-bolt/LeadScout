import { useEffect, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { request } from "../../shared/http/client";

export type Role = "ROOT" | "ADMIN" | "USER";
export type Member = { cleanup_action_id?: string; telegram_id: string; role: Role; access_status: "ACTIVE" | "BLOCKED"; display_label: string; telegram_name: string; telegram_username: string; revision: number; created_at: string; last_login_at?: string; account_count: number; task_count: number };
export type Task = { id: string; kind: string; user_id: string; account_id?: number; status: string; code: string; message: string; created_at: string; started_at?: string; finished_at?: string; can_stop: boolean };
export type Target = { task_id?: string; user_id: string; kind: string; status?: string; code?: string };
export type Action = { id: string; actor_id: string; kind: string; target_id?: string; status: string; created_at: string; targets: Target[]; results: Target[] };
export type TechnicalError = { id: string; component: string; code: string; message: string; task_id?: string; correlation_id: string; created_at: string };
export type Overview = { api: { started_at: string; uptime_seconds: number }; database: { ok: boolean }; bot: { polling: boolean }; scheduler: { running: boolean; next_run_at?: string }; ai: { status: string; checked_at?: string }; tasks: Record<string, number>; errors_24h: number };
export type Page<T> = { items: T[]; next_offset: number | null; stop_scope?: { users: number; tasks: number; accounts: number } };
export const roleLabel: Record<Role, string> = { ROOT: "Главный администратор", ADMIN: "Администратор", USER: "Пользователь" };
export const labels: Record<string, string> = { QUEUED: "В очереди", RUNNING: "Выполняется", WAITING_INPUT: "Нужен ввод", STOPPING: "Останавливается", SUCCEEDED: "Завершено", FAILED: "Ошибка", CANCELLED: "Отменено", INTERRUPTED: "Прервано", PENDING: "Ожидает выполнения", NEEDS_CLEANUP: "Требуется завершить остановку", STOPPED: "Остановлено", search: "Поиск и отклики", questionnaire: "Отправка анкеты", login: "Вход в hh.ru", "resume-sync": "Синхронизация резюме", "resume-import": "Импорт резюме", "resume-delete": "Удаление резюме", "resume-audit": "ИИ-аудит", "pdf-resume-audit": "Аудит PDF", "vacancy-match": "Сравнение с вакансией", ADD_MEMBER: "Добавление человека", PATCH_MEMBER: "Изменение прав или подписи", BLOCK: "Отключение доступа", RESTORE: "Восстановление доступа", STOP_TASK: "Остановка задания", STOP_ALL: "Массовая остановка", RETRY_CLEANUP: "Повтор завершения остановки", user_resources: "Ресурсы входа" };
export function title(value: string) { return labels[value] ?? value; }
export function useVisible() {
  const [visible, setVisible] = useState(document.visibilityState === "visible");
  useEffect(() => { const update = () => setVisible(document.visibilityState === "visible"); document.addEventListener("visibilitychange", update); return () => document.removeEventListener("visibilitychange", update); }, []);
  return visible;
}
export function useAdminList<T>(path: string, interval?: number) {
  const visible = useVisible();
  return useInfiniteQuery({ queryKey: ["admin", path], initialPageParam: 0, queryFn: ({ pageParam }) => request<Page<T>>(`/admin/${path}${path.includes("?") ? "&" : "?"}offset=${pageParam}&limit=50`), getNextPageParam: (last) => last.next_offset ?? undefined, enabled: visible, refetchInterval: visible && interval ? interval : false, retry: false });
}
export function useAdminMutation() {
  const client = useQueryClient();
  const pendingKeys = useRef(new Map<string, string>());
  return useMutation({
    mutationFn: async ({ path, method = "POST", body }: { path: string; method?: string; body?: unknown }) => {
      const fingerprint = JSON.stringify([path, method, body]);
      let id = pendingKeys.current.get(fingerprint);
      if (!id) {
        id = Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
        pendingKeys.current.set(fingerprint, id);
      }
      const result = await request<Action | Member>(`/admin/${path}`, { method, headers: { "Idempotency-Key": id }, body: body === undefined ? undefined : JSON.stringify(body) });
      pendingKeys.current.delete(fingerprint);
      return result;
    },
    onSettled: async () => { await Promise.all([client.invalidateQueries({ queryKey: ["admin"] }), client.invalidateQueries({ queryKey: ["dashboard"] })]); },
  });
}
