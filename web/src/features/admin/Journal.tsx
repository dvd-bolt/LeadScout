import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { request } from "../../shared/http/client";
import { Button, Card, Message } from "../../shared/ui";
import { formatDate } from "../../shared/lib/format";
import { title, useAdminList, useAdminMutation, useVisible, type Action, type TechnicalError } from "./model";
import ui from "../../shared/ui/UI.module.css";
import styles from "./Admin.module.css";

function ActionCard({ initial }: { initial: Action }) {
  const visible = useVisible();
  const [retryConfirm, setRetryConfirm] = useState(false);
  const query = useQuery({ queryKey: ["admin", "action", initial.id], queryFn: () => request<Action>(`/admin/actions/${initial.id}`), initialData: initial, enabled: visible && ["PENDING", "RUNNING", "NEEDS_CLEANUP"].includes(initial.status), refetchInterval: (q) => visible && ["PENDING", "RUNNING"].includes(q.state.data?.status ?? "") ? 3000 : false, retry: false });
  const action = initial.status === "SUCCEEDED" ? initial : query.data;
  const mutation = useAdminMutation();
  return <Card><div className={styles.stack}><h3>{title(action.kind)}</h3><div className={styles.small}>Инициатор: {action.actor_id}{action.target_id ? ` · Цель: ${action.target_id}` : ""}<br />{formatDate(action.created_at)} МСК<br />ID: {action.id}</div><strong>{action.kind === "BLOCK" ? `Доступ отключён. ${action.status === "SUCCEEDED" ? "Остановка завершена." : action.status === "NEEDS_CLEANUP" ? "Не удалось завершить остановку всех ресурсов." : "Остановка ещё выполняется."}` : title(action.status)}</strong>
    {action.results.map((result, i) => <div key={i} className={styles.small}>{result.user_id} · {title(result.kind)}{result.task_id ? ` · ${result.task_id}` : ""}<br /><strong>{title(result.status ?? "PENDING")}</strong>{result.code ? ` · ${result.code}` : ""}</div>)}
    {action.status === "NEEDS_CLEANUP" ? <><Button className={ui.secondary} disabled={mutation.isPending} onClick={() => setRetryConfirm(true)}>Повторить завершение остановки</Button>{retryConfirm ? <div className={styles.confirm}><p>Повторить закрытие оставшихся ресурсов? Доступ не будет восстановлен.</p><Button disabled={mutation.isPending} onClick={() => mutation.mutate({ path: `actions/${action.id}/retry-cleanup` }, { onSettled: () => setRetryConfirm(false) })}>Подтвердить повтор остановки</Button><Button className={ui.secondary} disabled={mutation.isPending} onClick={() => setRetryConfirm(false)}>Отмена</Button></div> : null}</> : null}
    <Message notice={mutation.error || query.error ? { text: (mutation.error || query.error)!.message, error: true } : null} /></div></Card>;
}

export function Journal() {
  const actions = useAdminList<Action>("actions");
  const errors = useAdminList<TechnicalError>("errors");
  return <div className={styles.stack}><Button className={ui.secondary} disabled={actions.isFetching || errors.isFetching} onClick={() => { void actions.refetch(); void errors.refetch(); }}>Обновить журнал</Button><h2>Действия администраторов</h2>
    <Message notice={actions.error ? { text: actions.error.message, error: true } : actions.isPending ? { text: "Загружаем журнал…" } : null} />
    {actions.data?.pages.flatMap((p) => p.items).map((a) => <ActionCard key={a.id} initial={a} />)}
    {actions.data?.pages[0].items.length === 0 ? <Card>Действий пока нет.</Card> : null}{actions.hasNextPage ? <Button disabled={actions.isFetchingNextPage} onClick={() => void actions.fetchNextPage()}>Показать ещё действия</Button> : null}
    <h2>Технические ошибки</h2><Message notice={errors.error ? { text: errors.error.message, error: true } : null} />
    {errors.data?.pages.flatMap((p) => p.items).map((error) => <Card key={error.id}><div className={styles.stack}><h3>{error.component} · {error.code}</h3><p>{error.message}</p><div className={styles.small}>{formatDate(error.created_at)} МСК<br />{error.task_id ? `Задание: ${error.task_id}` : ""}<br />{error.correlation_id ? `Correlation ID: ${error.correlation_id}` : ""}</div></div></Card>)}
    {errors.data?.pages[0].items.length === 0 ? <Card>Технических ошибок пока нет.</Card> : null}{errors.hasNextPage ? <Button disabled={errors.isFetchingNextPage} onClick={() => void errors.fetchNextPage()}>Показать ещё ошибки</Button> : null}
  </div>;
}
