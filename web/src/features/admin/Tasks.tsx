import { useState } from "react";
import { Button, Card, Field, Message } from "../../shared/ui";
import { formatDate } from "../../shared/lib/format";
import { title, useAdminList, useAdminMutation, type Role, type Task } from "./model";
import ui from "../../shared/ui/UI.module.css";
import styles from "./Admin.module.css";

export function Tasks({ role }: { role: Role }) {
  const [status, setStatus] = useState("");
  const [userId, setUserId] = useState("");
  const [confirm, setConfirm] = useState<Task | "all" | null>(null);
  const list = useAdminList<Task>(`tasks?${status ? `status=${status}` : ""}${userId ? `&user_id=${encodeURIComponent(userId)}` : ""}`, 3000);
  const mutation = useAdminMutation();
  const scope = list.data?.pages[0].stop_scope;
  return <div className={styles.stack}>
    <Button className={ui.dangerButton} disabled={!scope || mutation.isPending} onClick={() => setConfirm("all")}>{role === "ROOT" ? "Остановить все задания" : "Остановить все, кроме главного администратора"}</Button>
    {confirm ? <div className={styles.confirm} role="group" aria-label="Подтверждение остановки"><p>{confirm === "all" ? `Остановить задания: ${scope?.tasks ?? "—"}, затронутых людей: ${scope?.users ?? "—"}? Автопоиск будет выключен у ${scope?.accounts ?? "—"} аккаунтов. ${role === "ROOT" ? "Включая ваши задания." : "Работа главного администратора сохранится."}` : `Остановить «${title(confirm.kind)}», владелец ${confirm.user_id}? ${confirm.kind === "search" ? "Автопоиск аккаунта будет выключен." : "Независимые задания продолжат работу."}`}</p><p className={styles.meta}>Уже выполненные действия на hh.ru не отменяются. Перед повторной отправкой проверьте внешний результат.</p><div className={ui.actionRow}><Button disabled={mutation.isPending} onClick={() => mutation.mutate({ path: confirm === "all" ? "tasks/stop-all" : `tasks/${confirm.id}/stop` }, { onSettled: () => setConfirm(null) })}>{mutation.isPending ? "Принимаем команду…" : "Подтвердить остановку"}</Button><Button className={ui.secondary} disabled={mutation.isPending} onClick={() => setConfirm(null)}>Отмена</Button></div></div> : null}
    <Message notice={mutation.error ? { text: mutation.error.message, error: true } : mutation.isSuccess ? { text: "Команда принята. Результаты остановки — во вкладке «Журнал»." } : null} />
    <div className={styles.grid}><Field label="Состояние задания"><select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">Все состояния</option>{["QUEUED", "RUNNING", "WAITING_INPUT", "STOPPING", "SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"].map((s) => <option key={s} value={s}>{title(s)}</option>)}</select></Field><Field label="ID владельца"><input inputMode="numeric" value={userId} onChange={(e) => setUserId(e.target.value)} /></Field></div>
    <Button className={ui.secondary} disabled={list.isFetching} onClick={() => void list.refetch()}>Обновить задания</Button>
    <Message notice={list.error ? { text: list.error.message, error: true } : list.isPending ? { text: "Загружаем задания…" } : null} />
    {list.data?.pages.flatMap((page) => page.items).map((task) => <Card key={task.id}><div className={styles.stack}><h3>{title(task.kind)}</h3><strong>{title(task.status)}</strong><div className={styles.small}>Задание: {task.id}<br />Владелец: <span className={styles.id}>{task.user_id}</span><br />Аккаунт: {task.account_id ?? "—"}<br />Создано: {formatDate(task.created_at)} МСК<br />Начало: {formatDate(task.started_at)}{task.started_at ? " МСК" : ""}<br />Длительность: {task.started_at ? `${Math.max(0, Math.floor(((task.finished_at ? Date.parse(task.finished_at) : Date.now()) - Date.parse(task.started_at)) / 1000))} сек.` : "—"}</div><Message notice={task.message ? { text: task.message, error: task.status === "FAILED" } : null} />{task.can_stop ? <Button className={ui.dangerButton} disabled={mutation.isPending} onClick={() => setConfirm(task)}>Остановить</Button> : null}</div></Card>)}
    {list.data?.pages[0].items.length === 0 ? <Card>Заданий по этим условиям нет. История накапливается с момента обновления.</Card> : null}
    {list.hasNextPage ? <Button disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>Показать ещё</Button> : null}
  </div>;
}
