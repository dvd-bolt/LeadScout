import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Card, Field, Message } from "../../shared/ui";
import { request } from "../../shared/http/client";
import { formatDate } from "../../shared/lib/format";
import { roleLabel, useAdminList, useAdminMutation, useVisible, type Action, type Member } from "./model";
import ui from "../../shared/ui/UI.module.css";
import styles from "./Admin.module.css";

function MemberCard({ member }: { member: Member }) {
  const [label, setLabel] = useState(member.display_label);
  const [confirm, setConfirm] = useState<{ path: string; method?: string; body: unknown; text: string } | null>(null);
  useEffect(() => setLabel(member.display_label), [member.display_label]);
  const mutation = useAdminMutation();
  const visible = useVisible();
  const cleanup = useQuery({ queryKey: ["admin", "action", member.cleanup_action_id], queryFn: () => request<Action>(`/admin/actions/${member.cleanup_action_id}`), enabled: visible && Boolean(member.cleanup_action_id), refetchInterval: (q) => visible && ["PENDING", "RUNNING"].includes(q.state.data?.status ?? "PENDING") ? 3000 : false, retry: false });
  const cleanupIncomplete = Boolean(member.cleanup_action_id) && cleanup.data?.status !== "SUCCEEDED";
  const protectedMember = member.role === "ROOT";
  const prepare = (action: string) => {
    const body: Record<string, unknown> = { expected_revision: member.revision };
    let path = `members/${member.telegram_id}`, method = "POST", text = "";
    if (action === "role") { method = "PATCH"; body.role = member.role === "ADMIN" ? "USER" : "ADMIN"; text = member.role === "ADMIN" ? "Снять административные права? Личный кабинет сохранится." : "Назначить администратором? Человек получит диагностику и остановку чужих заданий, кроме главного администратора."; }
    if (action === "label") { method = "PATCH"; body.display_label = label; text = "Сохранить внутреннюю подпись?"; }
    if (action === "block") { path += "/block"; text = `Закрыть доступ для Telegram ID ${member.telegram_id}? Человек не сможет пользоваться ботом и Mini App. Его задания будут остановлены, автопоиск выключен. Аккаунты, резюме и история сохранятся.`; }
    if (action === "restore") { path += "/restore"; text = "Восстановить доступ? Потребуется новый вход в Mini App. Автопоиск останется выключенным."; }
    setConfirm({ path, method, body, text });
  };
  return <Card><div className={styles.stack}>
    <div><div className={styles.id}>{member.telegram_id}</div><h3>{member.display_label || member.telegram_name || "Имя пока неизвестно"}</h3><div className={styles.meta}>{member.telegram_name} {member.telegram_username ? `@${member.telegram_username}` : ""}</div></div>
    <div>{roleLabel[member.role]} · <strong>{member.access_status === "ACTIVE" ? "Активен" : "Отключён"}</strong></div>
    <div className={styles.meta}>Добавлен: {formatDate(member.created_at)} МСК<br />Последний вход: {member.last_login_at ? `${formatDate(member.last_login_at)} МСК` : "Ещё не входил"}<br />Аккаунты hh.ru: {member.account_count} · Незавершённые задания: {member.task_count}</div>
    {member.cleanup_action_id ? <Message notice={{ text: cleanup.data?.status === "SUCCEEDED" ? "Доступ отключён. Остановка завершена." : cleanup.data?.status === "NEEDS_CLEANUP" ? "Доступ отключён. Не удалось завершить остановку. Повторите её во вкладке «Журнал»." : "Доступ отключён. Остановка ещё выполняется.", error: cleanup.data?.status === "NEEDS_CLEANUP" }} /> : null}
    {!protectedMember ? <><Field label="Внутренняя подпись"><input maxLength={100} value={label} onChange={(e) => setLabel(e.target.value)} /></Field><div className={ui.actionRow}>
      <Button className={ui.secondary} disabled={mutation.isPending || label === member.display_label} onClick={() => prepare("label")}>Сохранить подпись</Button>
      <Button className={ui.secondary} disabled={mutation.isPending} onClick={() => prepare("role")}>{member.role === "ADMIN" ? "Снять права администратора" : "Назначить администратором"}</Button>
      <Button className={ui.dangerButton} disabled={mutation.isPending || cleanupIncomplete} onClick={() => prepare(member.access_status === "ACTIVE" ? "block" : "restore")}>{member.access_status === "ACTIVE" ? "Отключить доступ" : "Восстановить доступ"}</Button>
    </div></> : <p className={styles.meta}>Главная роль защищена. Через интерфейс она не изменяется.</p>}
    {confirm ? <div className={styles.confirm} role="group" aria-label="Подтверждение изменения доступа"><p>{confirm.text}</p><div className={ui.actionRow}><Button disabled={mutation.isPending} onClick={() => mutation.mutate(confirm, { onSuccess: () => setConfirm(null), onError: () => setConfirm(null) })}>{mutation.isPending ? "Выполняется…" : "Подтвердить"}</Button><Button className={ui.secondary} disabled={mutation.isPending} onClick={() => setConfirm(null)}>Отмена</Button></div></div> : null}
    <Message notice={mutation.error ? { text: mutation.error.message, error: true } : mutation.isSuccess ? { text: member.access_status === "BLOCKED" ? "Доступ отключён. Результат остановки — во вкладке «Журнал»." : "Изменение принято.", tone: "success" } : null} />
  </div></Card>;
}

export function People() {
  const [search, setSearch] = useState("");
  const [role, setRole] = useState("");
  const [status, setStatus] = useState("");
  const [adding, setAdding] = useState(false);
  const [id, setId] = useState("");
  const [newRole, setNewRole] = useState("USER");
  const [label, setLabel] = useState("");
  const [confirm, setConfirm] = useState(false);
  const [copied, setCopied] = useState("");
  const list = useAdminList<Member>(`members?search=${encodeURIComponent(search)}${role ? `&role=${role}` : ""}${status ? `&access_status=${status}` : ""}`);
  const bot = useQuery({ queryKey: ["admin", "bot-link"], queryFn: () => request<{ bot_url: string }>("/admin/invitation"), enabled: adding });
  const mutation = useAdminMutation();
  return <div className={styles.stack}>
    <div className={ui.actionRow}><Button onClick={() => { setAdding(!adding); setConfirm(false); }}>Добавить человека</Button><Button className={ui.secondary} disabled={list.isFetching} onClick={() => void list.refetch()}>Обновить список</Button></div>
    {adding ? <Card><form className={ui.form} onSubmit={(e) => { e.preventDefault(); setConfirm(true); }}>
      <Field label="Telegram ID"><input required inputMode="numeric" pattern="[0-9]+" maxLength={19} value={id} onChange={(e) => { setId(e.target.value); setConfirm(false); }} /></Field>
      <Field label="Роль"><select value={newRole} onChange={(e) => { setNewRole(e.target.value); setConfirm(false); }}><option value="USER">Пользователь</option><option value="ADMIN">Администратор</option></select></Field>
      <Field label="Внутренняя подпись (необязательно)"><input maxLength={100} value={label} onChange={(e) => { setLabel(e.target.value); setConfirm(false); }} /></Field>
      <p className={styles.meta}>Добавление выдаёт доступ, но не проверяет существование Telegram-аккаунта. Приглашение автоматически не отправляется.</p>
      {!confirm ? <Button type="submit">Продолжить</Button> : <div className={styles.confirm}><p>Добавить <strong className={styles.id}>{id}</strong> с ролью «{newRole === "ADMIN" ? "Администратор" : "Пользователь"}»?</p><Button type="button" disabled={mutation.isPending} onClick={() => mutation.mutate({ path: "members", body: { telegram_id: id, role: newRole, display_label: label } }, { onSuccess: () => { setConfirm(false); setSearch(id); } })}>{mutation.isPending ? "Добавляем…" : "Подтвердить добавление"}</Button></div>}
      <Message notice={mutation.error ? { text: mutation.error.message, error: true } : mutation.isSuccess ? { text: "Доступ выдан. Передайте человеку ссылку на бота.", tone: "success" } : null} />
      {mutation.error ? <Button type="button" className={ui.secondary} onClick={() => { setSearch(id); setAdding(false); }}>Открыть существующую карточку</Button> : null}
      {mutation.isSuccess && bot.data?.bot_url ? <><a href={bot.data.bot_url} target="_blank" rel="noreferrer">Открыть бота</a><Button type="button" className={ui.secondary} onClick={() => { void (navigator.clipboard?.writeText?.(bot.data!.bot_url) ?? Promise.reject(new Error("Clipboard unavailable"))).then(() => setCopied("Ссылка скопирована"), () => setCopied("Не удалось скопировать. Используйте ссылку выше.")); }}>Копировать ссылку</Button><Message notice={copied ? { text: copied } : null} /></> : null}
    </form></Card> : null}
    <Field label="Поиск по ID, подписи, имени или username"><input value={search} onChange={(e) => setSearch(e.target.value)} /></Field>
    <div className={styles.grid}><Field label="Роль"><select value={role} onChange={(e) => setRole(e.target.value)}><option value="">Все роли</option>{Object.entries(roleLabel).map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></Field><Field label="Доступ"><select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">Все</option><option value="ACTIVE">Активные</option><option value="BLOCKED">Отключённые</option></select></Field></div>
    <Message notice={list.error ? { text: list.error.message, error: true } : list.isPending ? { text: "Загружаем людей…" } : null} />
    {list.data?.pages.flatMap((page) => page.items).map((member) => <MemberCard key={member.telegram_id} member={member} />)}
    {list.data?.pages[0].items.length === 0 ? <Card>Людей по этим условиям нет.</Card> : null}
    {list.hasNextPage ? <Button disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>Показать ещё</Button> : null}
  </div>;
}
