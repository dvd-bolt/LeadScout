import { FormEvent, useEffect, useRef, useState } from "react";
import { HashRouter, NavLink, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, authenticate, setCsrfToken } from "./api";
import { telegram } from "./telegram";
import type { Account, Audit, Dashboard, Operation, Questionnaire, Resume } from "./types";
import styles from "./App.module.css";

type Notice = { text: string; error?: boolean } | null;

function statusLabel(account: Account) {
  switch (account.automation_state) {
    case "SEARCHING": return { text: "Идёт поиск", tone: styles.good };
    case "QUEUED": return { text: "В очереди · автоматизация включена", tone: styles.good };
    case "NEEDS_LOGIN": return { text: "Нужен вход", tone: styles.warning };
    case "ERROR": return { text: "Ошибка", tone: styles.danger };
    default: return { text: "Ожидается следующий запуск", tone: "" };
  }
}

function formatDate(value?: string) {
  if (!value) return "—";
  const normalized = value.replace(" ", "T");
  const date = new Date(normalized + (/[zZ]|[+-]\d\d:\d\d$/.test(normalized) ? "" : "Z"));
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short" }).format(date);
}

function Button({ children, className = "", ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={`${styles.button} ${className}`} {...props}>{children}</button>;
}

function Message({ notice }: { notice: Notice }) {
  if (!notice) return null;
  return <div role="status" className={`${styles.notice} ${notice.error ? styles.error : ""}`}>{notice.text}</div>;
}

function OperationStatus({ operationId, onDone }: { operationId: string | null; onDone?: (operation: Operation) => void }) {
  const reported = useRef<string | null>(null);
  const query = useQuery({
    queryKey: ["operation", operationId],
    queryFn: () => api.operation(operationId!),
    enabled: Boolean(operationId),
    refetchInterval: (result) => result.state.data?.status === "RUNNING" || result.state.data?.status === "PENDING" ? 3000 : false,
  });
  useEffect(() => {
    if (query.data?.status === "SUCCEEDED" && reported.current !== query.data.id) {
      reported.current = query.data.id;
      onDone?.(query.data);
    }
  }, [query.data, onDone]);
  if (!operationId || !query.data) return null;
  const state = query.data as Operation;
  return <Message notice={{ text: state.status === "FAILED" ? state.error_text : state.status === "SUCCEEDED" ? "Операция завершена" : "Операция выполняется…", error: state.status === "FAILED" }} />;
}

function Layout({ dashboard, children }: { dashboard: Dashboard; children: React.ReactNode }) {
  const client = useQueryClient();
  const location = useLocation();
  const active = dashboard.accounts.find((item) => item.id === dashboard.active_account_id);
  const activate = useMutation({
    mutationFn: api.activateAccount,
    onSuccess: () => client.invalidateQueries({ queryKey: ["dashboard"] }),
  });
  useEffect(() => {
    const app = telegram();
    if (!app) return;
    const goBack = () => history.back();
    if (location.pathname !== "/") app.BackButton.show(); else app.BackButton.hide();
    app.BackButton.onClick(goBack);
    return () => app.BackButton.offClick(goBack);
  }, [location.pathname]);
  return <main className={styles.shell}>
    <header className={styles.header}>
      <div className={styles.brand}><span className={styles.mark}>↗</span><span>LeadScout</span></div>
      {dashboard.accounts.length > 0 && <select aria-label="Активный аккаунт" className={styles.accountSelect} value={active?.id ?? ""} onChange={(event) => activate.mutate(Number(event.target.value))}>
        {dashboard.accounts.map((account) => <option value={account.id} key={account.id}>{account.account_name}</option>)}
      </select>}
    </header>
    {children}
    <nav aria-label="Основная навигация" className={styles.bottomNav}>
      <NavItem to="/" label="Главная" end />
      <NavItem to="/applications" label={dashboard.pending_review_count ? `Отклики · ${dashboard.pending_review_count}` : "Отклики"} />
      <NavItem to="/resumes" label="Резюме" />
      <NavItem to="/settings" label="Настройки" />
    </nav>
  </main>;
}

function NavItem({ to, label, end = false }: { to: string; label: string; end?: boolean }) {
  return <NavLink to={to} end={end} className={({ isActive }) => `${styles.navLink} ${isActive ? styles.navLinkActive : ""}`}>{label}</NavLink>;
}

function Home({ dashboard }: { dashboard: Dashboard }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [notice, setNotice] = useState<Notice>(null);
  const active = dashboard.accounts.find((account) => account.id === dashboard.active_account_id);
  const automation = useMutation({
    mutationFn: () => active?.auto_apply_enabled ? api.stopAutomation(active.id) : api.startAutomation(active!.id),
    onSuccess: () => { client.invalidateQueries({ queryKey: ["dashboard"] }); setNotice({ text: "Настройка автоматизации обновлена" }); },
    onError: (error) => setNotice({ text: error.message, error: true }),
  });
  const stopAll = useMutation({
    mutationFn: api.stopAll,
    onSuccess: () => { client.invalidateQueries({ queryKey: ["dashboard"] }); setNotice({ text: "Все автоматизации остановлены" }); },
    onError: (error) => setNotice({ text: error.message, error: true }),
  });
  if (!active) return <section className={styles.page}><div className={styles.hero}><h1>Начнём поиск</h1><p>Подключи hh.ru, выбери резюме и настрой первые отклики.</p></div><SetupChecklist onOpen={() => navigate("/settings")} /></section>;
  const currentStatus = statusLabel(active);
  return <section className={styles.page}>
    <div className={styles.hero}><h1>Поиск под контролем</h1><p>{active.active_resume_title || "Выбери резюме для поиска"}</p></div>
    <Message notice={notice} />
    <section className={styles.card}>
      <div className={styles.cardHeader}><h2>{active.account_name}</h2><span className={`${styles.status} ${currentStatus.tone}`}>● {currentStatus.text}</span></div>
      <div className={styles.statGrid}>
        <div className={styles.stat}><strong>{active.applied_today}</strong><span>из {active.daily_limit} сегодня</span></div>
        <div className={styles.stat}><strong>{dashboard.pending_review_count}</strong><span>нуждается в проверке</span></div>
        <div className={styles.stat}><strong>{formatDate(dashboard.next_scheduled_search_at)}</strong><span>следующий запуск</span></div>
      </div>
      <details style={{ marginTop: 12 }}><summary>Параметры запуска</summary><div className={styles.meta}>Аккаунт: {active.account_name}<br />Резюме: {active.active_resume_title || "не выбрано"}<br />Ключевые слова: {active.keywords || "не заданы"}<br />Минимальная зарплата: {active.min_salary ? `${active.min_salary.toLocaleString("ru-RU")} ₽` : "не задана"}<br />Формат: {active.only_remote ? "удалённая работа" : "любой"}<br />Лимит: {active.daily_limit} в день</div></details>
      <div className={styles.actionRow} style={{ marginTop: 14 }}>
        <Button disabled={automation.isPending || !active.resume_ready || active.session_status !== "ACTIVE"} onClick={() => automation.mutate()}>{active.auto_apply_enabled ? "Остановить поиск" : "Запустить поиск"}</Button>
        <Button className={`${styles.secondary} ${styles.dangerButton}`} disabled={stopAll.isPending} onClick={() => stopAll.mutate()}>Остановить всё</Button>
      </div>
      {!active.resume_ready && <div className={styles.notice} style={{ marginTop: 12 }}>Выбери синхронизированное резюме с текстом перед запуском.</div>}
    </section>
    <section className={styles.card}><div className={styles.cardHeader}><h2>Последние события</h2><button className={styles.textButton} onClick={() => navigate("/applications")}>Все отклики</button></div>
      {dashboard.recent_events.length ? <div className={styles.eventList}>{dashboard.recent_events.map((item) => <EventRow event={item} key={item.id} />)}</div> : <div className={styles.empty}>Событий ещё нет. После запуска здесь появится история поиска.</div>}
    </section>
  </section>;
}

function SetupChecklist({ onOpen }: { onOpen: () => void }) {
  return <section className={styles.card}><h2>Первый запуск</h2><div className={styles.eventList} style={{ marginTop: 14 }}><div>1. Подключи аккаунт hh.ru</div><div>2. Синхронизируй и выбери резюме</div><div>3. Задай параметры поиска и лимит</div><Button onClick={onOpen}>Открыть настройки</Button></div></section>;
}

function EventRow({ event }: { event: Dashboard["recent_events"][number] }) {
  return <div className={styles.event}><div className={styles.eventTitle}>{event.vacancy_title || "Вакансия"}</div><div className={styles.meta}>{event.company || "Компания не указана"} · {event.status} · {formatDate(event.created_at)}</div></div>;
}

function Applications({ dashboard }: { dashboard: Dashboard }) {
  const activeId = dashboard.active_account_id ?? undefined;
  const [tab, setTab] = useState<"review" | "history">("review");
  const query = useQuery({ queryKey: ["questionnaires", activeId], queryFn: () => api.questionnaires(activeId), refetchInterval: () => document.visibilityState === "visible" ? 15000 : false });
  const history = useQuery({ queryKey: ["applications", activeId], queryFn: () => api.applications(activeId), enabled: tab === "history" });
  return <section className={styles.page}>
    <div className={styles.tabs}><button className={`${styles.tab} ${tab === "review" ? styles.tabActive : ""}`} onClick={() => setTab("review")}>Нужна проверка</button><button className={`${styles.tab} ${tab === "history" ? styles.tabActive : ""}`} onClick={() => setTab("history")}>История</button></div>
    {tab === "review" ? <QuestionnaireList items={query.data ?? []} /> : <section className={styles.card}><h2>История откликов</h2><div className={styles.eventList} style={{ marginTop: 14 }}>{history.data?.history.map((event) => <EventRow event={event} key={event.id} />) || <div className={styles.empty}>Пока пусто.</div>}</div></section>}
  </section>;
}

function QuestionnaireList({ items }: { items: Questionnaire[] }) {
  const client = useQueryClient();
  const [notice, setNotice] = useState<Notice>(null);
  const action = useMutation({
    mutationFn: ({ id, values }: { id: number; values?: { cover_letter?: string; answers?: Questionnaire["ai_payload"]["answers"] } }) => values ? api.updateQuestionnaire(id, values) : api.confirmQuestionnaire(id),
    onSuccess: () => { client.invalidateQueries({ queryKey: ["questionnaires"] }); setNotice({ text: "Анкета обновлена" }); },
    onError: (error) => setNotice({ text: error.message, error: true }),
  });
  return <><Message notice={notice} /><section className={styles.questionList}>{items.length ? items.filter((item) => item.status !== "SUBMITTED").map((item) => <QuestionnaireCard item={item} key={item.id} busy={action.isPending} onSave={(letter, answers) => action.mutate({ id: item.id, values: { cover_letter: letter, answers } })} onConfirm={() => action.mutate({ id: item.id })} />) : <div className={`${styles.card} ${styles.empty}`}>Нет анкет, ожидающих решения.</div>}</section></>;
}

function QuestionnaireCard({ item, busy, onSave, onConfirm }: { item: Questionnaire; busy: boolean; onSave: (letter: string, answers: NonNullable<Questionnaire["ai_payload"]["answers"]>) => void; onConfirm: () => void }) {
  const [letter, setLetter] = useState(item.cover_letter);
  const [answers, setAnswers] = useState(() => Object.fromEntries((item.ai_payload.answers ?? []).map((answer) => [answer.field_id, answer.value])));
  const savedAnswers = item.questions.map((question) => ({ field_id: question.field_id, value: answers[question.field_id] ?? "", answer_type: question.answer_type }));
  return <article className={styles.question}>
    <div className={styles.questionTitle}>{item.vacancy_title || "Вакансия"}</div><div className={styles.meta}>Резюме: {item.resume_title || "не определено"} · {item.status}</div>
    {item.error_text && <div className={`${styles.notice} ${styles.error}`} style={{ marginTop: 10 }}>{item.error_text}</div>}
    <label className={styles.field} style={{ marginTop: 12 }}>Сопроводительное письмо<textarea value={letter} onChange={(event) => setLetter(event.target.value)} /></label>
    {item.questions.length > 0 && <details><summary>Ответы на вопросы ({item.questions.length})</summary><div className={styles.form} style={{ marginTop: 10 }}>{item.questions.map((question) => <label className={styles.field} key={question.field_id}>{question.required ? "* " : ""}{question.label}{question.options.length ? <select value={answers[question.field_id] ?? ""} onChange={(event) => setAnswers({ ...answers, [question.field_id]: event.target.value })}><option value="">Выберите ответ</option>{question.options.map((option) => <option value={option} key={option}>{option}</option>)}</select> : <input value={answers[question.field_id] ?? ""} onChange={(event) => setAnswers({ ...answers, [question.field_id]: event.target.value })} />}</label>)}</div></details>}
    <div className={styles.actionRow} style={{ marginTop: 12 }}><Button className={styles.secondary} disabled={busy} onClick={() => onSave(letter, savedAnswers)}>Сохранить</Button><Button disabled={busy} onClick={onConfirm}>Подтвердить отправку</Button></div>
  </article>;
}

function Resumes({ dashboard }: { dashboard: Dashboard }) {
  const accountId = dashboard.active_account_id;
  const client = useQueryClient();
  const [operation, setOperation] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const resumes = useQuery({ queryKey: ["resumes", accountId], queryFn: () => api.resumes(accountId!), enabled: accountId !== null });
  const audits = useQuery({ queryKey: ["audits", accountId], queryFn: () => api.audits(accountId!), enabled: accountId !== null });
  const select = useMutation({ mutationFn: (id: number) => api.activateResume(accountId!, id), onSuccess: () => client.invalidateQueries({ queryKey: ["dashboard"] }) });
  const sync = useMutation({ mutationFn: () => api.syncResumes(accountId!), onSuccess: (data) => setOperation(data.operation_id), onError: (error) => setNotice({ text: error.message, error: true }) });
  const upload = useMutation({ mutationFn: (file: File) => api.importResume(accountId!, file), onSuccess: (data) => setOperation(data.operation_id), onError: (error) => setNotice({ text: error.message, error: true }) });
  const audit = useMutation({ mutationFn: () => api.createAudit({ account_id: accountId! }), onSuccess: (data) => setOperation(data.operation_id), onError: (error) => setNotice({ text: error.message, error: true }) });
  const auditPdf = useMutation({ mutationFn: (file: File) => api.createPdfAudit(accountId!, file), onSuccess: (data) => setOperation(data.operation_id), onError: (error) => setNotice({ text: error.message, error: true }) });
  const deleteResume = useMutation({ mutationFn: (id: number) => api.deleteResume(accountId!, id), onSuccess: (data) => setOperation(data.operation_id), onError: (error) => setNotice({ text: error.message, error: true }) });
  if (!accountId) return <section className={`${styles.card} ${styles.empty}`}>Сначала подключи аккаунт hh.ru.</section>;
  return <section className={styles.page}><Message notice={notice} /><OperationStatus operationId={operation} onDone={() => { client.invalidateQueries({ queryKey: ["resumes"] }); client.invalidateQueries({ queryKey: ["audits"] }); client.invalidateQueries({ queryKey: ["dashboard"] }); }} />
    <section className={styles.card}><div className={styles.cardHeader}><h2>Резюме hh.ru</h2><Button className={styles.secondary} disabled={sync.isPending} onClick={() => sync.mutate()}>Синхронизировать</Button></div>
      <label className={styles.field}>Импорт нового PDF<input accept="application/pdf" type="file" onChange={(event) => event.target.files?.[0] && upload.mutate(event.target.files[0])} /></label>
      <div className={styles.resumeList} style={{ marginTop: 14 }}>{resumes.data?.length ? resumes.data.map((resume) => <ResumeRow resume={resume} active={dashboard.accounts.find((item) => item.id === accountId)?.active_resume_hh_id === resume.hh_resume_id} onSelect={() => select.mutate(resume.snapshot_id)} onDelete={() => { if (window.confirm(`Удалить «${resume.title}» на hh.ru? Это действие нельзя отменить.`)) deleteResume.mutate(resume.snapshot_id); }} key={resume.snapshot_id} />) : <div className={styles.empty}>Нажми «Синхронизировать», чтобы загрузить список.</div>}</div>
    </section>
    <section className={styles.card}><div className={styles.cardHeader}><h2>ИИ-аудит</h2><Button disabled={audit.isPending || !resumes.data?.length} onClick={() => audit.mutate()}>Проверить резюме</Button></div><details><summary>Проверить отдельный текст или PDF</summary><div className={styles.form} style={{ marginTop: 12 }}><CustomTextAudit accountId={accountId} onOperation={setOperation} onError={(text) => setNotice({ text, error: true })} /><label className={styles.field}>PDF только для аудита<input accept="application/pdf" type="file" disabled={auditPdf.isPending} onChange={(event) => event.target.files?.[0] && auditPdf.mutate(event.target.files[0])} /></label></div></details><AuditList audits={audits.data ?? []} /></section>
  </section>;
}

function CustomTextAudit({ accountId, onOperation, onError }: { accountId: number; onOperation: (id: string) => void; onError: (text: string) => void }) {
  const [text, setText] = useState("");
  const audit = useMutation({ mutationFn: () => api.createAudit({ account_id: accountId, resume_text: text }), onSuccess: (data) => onOperation(data.operation_id), onError: (error) => onError(error.message) });
  return <label className={styles.field}>Текст резюме<textarea value={text} placeholder="Вставь текст для отдельного аудита" onChange={(event) => setText(event.target.value)} /><Button className={styles.secondary} disabled={text.trim().length < 50 || audit.isPending} onClick={() => audit.mutate()}>Проверить текст</Button></label>;
}

function ResumeRow({ resume, active, onSelect, onDelete }: { resume: Resume; active: boolean; onSelect: () => void; onDelete: () => void }) {
  return <article className={styles.resume}><div className={styles.row}><div><div className={styles.resumeTitle}>{resume.title}</div><div className={styles.meta}>{resume.status} · синхр. {formatDate(resume.synced_at)}</div></div><div className={styles.actionRow}>{active ? <span className={`${styles.status} ${styles.good}`}>● Выбрано</span> : <Button className={styles.secondary} onClick={onSelect}>Выбрать</Button>}<button aria-label={`Удалить ${resume.title}`} className={`${styles.textButton} ${styles.danger}`} onClick={onDelete}>Удалить</button></div></div>{resume.extracted_text && <details><summary>Посмотреть текст</summary><p className={styles.meta}>{resume.extracted_text.slice(0, 800)}{resume.extracted_text.length > 800 ? "…" : ""}</p></details>}</article>;
}

function MatchSummary({ result }: { result: Record<string, unknown> }) {
  const skills = (name: string) => Array.isArray(result[name]) ? result[name].map((item) => typeof item === "object" && item ? String((item as { value?: unknown }).value ?? "") : String(item)).filter(Boolean).join(", ") : "—";
  return <details className={styles.notice} open><summary>Результат сравнения</summary><div className={styles.matchSummary}><strong>{String(result.match_score ?? "—")}/100 · {result.is_suitable ? "Можно откликаться" : "Есть критические несовпадения"}</strong><div><b>Совпадает:</b> {skills("matching_skills")}</div><div><b>Не хватает:</b> {skills("missing_skills")}</div><div>{String(result.advice_for_apply ?? "")}</div></div></details>;
}

function AuditList({ audits }: { audits: Audit[] }) {
  const [operation, setOperation] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [matchResult, setMatchResult] = useState<Record<string, unknown> | null>(null);
  const match = useMutation({ mutationFn: ({ id, value }: { id: number; value: string }) => api.matchAudit(id, value), onSuccess: (data) => setOperation(data.operation_id) });
  return <div className={styles.auditList}><OperationStatus operationId={operation} onDone={(result) => setMatchResult(result.result)} />{matchResult && <MatchSummary result={matchResult} />}{audits.length ? audits.map((audit) => <article className={styles.audit} key={audit.id}><div className={styles.row}><div><div className={styles.resumeTitle}>{audit.profession_name}</div><div className={styles.meta}>{formatDate(audit.created_at)}</div></div><span className={styles.score}>{audit.overall_score}</span></div><p className={styles.meta}>{audit.summary_text}</p><details><summary>Оценки и рекомендации</summary><div className={styles.meta}>{Object.entries(audit.category_scores).map(([name, score]) => <div key={name}>{name}: {score}/100</div>)}</div>{audit.top_recommendations.length > 0 && <ul className={styles.recommendations}>{audit.top_recommendations.map((item) => <li key={item}>{item}</li>)}</ul>}</details><a className={styles.textButton} href={`/api/v1/audits/${audit.id}/report`} target="_blank" rel="noreferrer">PDF-отчёт</a><label className={styles.field}>Сравнить с вакансией<textarea value={text} placeholder="Вставь описание вакансии" onChange={(event) => setText(event.target.value)} /></label><Button className={styles.secondary} disabled={text.trim().length < 15 || match.isPending} onClick={() => { setMatchResult(null); match.mutate({ id: audit.id, value: text }); }}>Сравнить</Button></article>) : <div className={styles.empty}>Аудитов ещё нет.</div>}</div>;
}

function Settings({ dashboard }: { dashboard: Dashboard }) {
  const active = dashboard.accounts.find((item) => item.id === dashboard.active_account_id);
  return <section className={styles.page}>{active ? <AccountSettings account={active} /> : null}<LoginPanel /></section>;
}

function AccountSettings({ account }: { account: Account }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [notice, setNotice] = useState<Notice>(null);
  const mutation = useMutation({ mutationFn: (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const proxyUrl = String(form.get("proxy_url") || "").trim();
    return api.patchAccount(account.id, {
      account_name: String(form.get("account_name")), keywords: String(form.get("keywords")), stop_words: String(form.get("stop_words")),
      min_salary: Number(form.get("min_salary") || 0), daily_limit: Number(form.get("daily_limit")),
      only_remote: form.get("only_remote") === "on", send_cover_letter: form.get("send_cover_letter") === "on", ...(proxyUrl ? { proxy_url: proxyUrl } : {}),
    });
  }, onSuccess: () => { client.invalidateQueries({ queryKey: ["dashboard"] }); setNotice({ text: "Настройки сохранены" }); }, onError: (error) => setNotice({ text: error.message, error: true }) });
  const remove = useMutation({ mutationFn: () => api.deleteAccount(account.id), onSuccess: () => { client.invalidateQueries({ queryKey: ["dashboard"] }); navigate("/"); }, onError: (error) => setNotice({ text: error.message, error: true }) });
  return <section className={styles.card}><div className={styles.cardHeader}><h2>Настройки поиска</h2><span className={styles.status}>{account.has_proxy ? "Прокси задан" : "Без прокси"}</span></div><Message notice={notice} /><form className={styles.form} onSubmit={(event) => mutation.mutate(event)} key={account.id}>
    <label className={styles.field}>Название аккаунта<input defaultValue={account.account_name} name="account_name" /></label>
    <label className={styles.field}>Ключевые слова<input defaultValue={account.keywords} name="keywords" placeholder="Python, backend" /></label>
    <label className={styles.field}>Стоп-слова<input defaultValue={account.stop_words} name="stop_words" placeholder="стажёр, продажи" /></label>
    <div className={styles.actionRow}><label className={styles.field}>Мин. зарплата<input defaultValue={account.min_salary || ""} min="0" name="min_salary" type="number" /></label><label className={styles.field}>Лимит в день<input defaultValue={account.daily_limit} min="1" max="200" name="daily_limit" type="number" /></label></div>
    <label className={styles.switch}><input defaultChecked={account.only_remote} name="only_remote" type="checkbox" /> Только удалённая работа</label><label className={styles.switch}><input defaultChecked={account.send_cover_letter} name="send_cover_letter" type="checkbox" /> Добавлять сопроводительное письмо</label><details><summary>Дополнительные параметры</summary><label className={styles.field} style={{ marginTop: 10 }}>Прокси URL<input name="proxy_url" placeholder={account.has_proxy ? "Прокси задан — вставь новый URL для замены" : "http://user:password@host:port"} /></label></details><Button disabled={mutation.isPending} type="submit">Сохранить настройки</Button>
  </form><Button className={`${styles.secondary} ${styles.dangerButton}`} disabled={remove.isPending} onClick={() => { if (window.confirm(`Удалить аккаунт «${account.account_name}» и его локальные данные?`)) remove.mutate(); }}>Удалить аккаунт</Button></section>;
}

function LoginPanel() {
  const client = useQueryClient();
  const [login, setLogin] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<{ status: string; captcha?: string; message?: string } | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const start = useMutation({ mutationFn: () => api.startLogin(login), onSuccess: (data) => setStep({ status: data.status, captcha: data.captcha_data_uri, message: data.message }), onError: (error) => setNotice({ text: error.message, error: true }) });
  const submit = useMutation({ mutationFn: () => step?.status === "WAITING_FOR_CAPTCHA" ? api.submitCaptcha(code) : api.submitOtp(code), onSuccess: (data) => { setStep({ status: data.status, captcha: data.captcha_data_uri, message: data.message }); setCode(""); if (data.status === "SUCCESS") client.invalidateQueries({ queryKey: ["dashboard"] }); }, onError: (error) => setNotice({ text: error.message, error: true }) });
  const reload = useMutation({ mutationFn: api.reloadCaptcha, onSuccess: (data) => setStep({ status: "WAITING_FOR_CAPTCHA", captcha: data.captcha_data_uri }), onError: (error) => setNotice({ text: error.message, error: true }) });
  const cancel = useMutation({ mutationFn: api.cancelLogin, onSuccess: () => { setStep(null); setCode(""); setNotice({ text: "Вход отменён" }); }, onError: (error) => setNotice({ text: error.message, error: true }) });
  return <section className={styles.card}><h2>Подключить или обновить вход</h2><p className={styles.meta}>Повторный вход использует тот же аккаунт и сохраняет его историю.</p><Message notice={notice} />
    {!step || step.status === "SUCCESS" ? <form className={styles.form} onSubmit={(event) => { event.preventDefault(); start.mutate(); }}><label className={styles.field}>Телефон или email hh.ru<input required value={login} onChange={(event) => setLogin(event.target.value)} /></label><Button disabled={start.isPending} type="submit">Продолжить</Button></form> : <form className={styles.form} onSubmit={(event) => { event.preventDefault(); submit.mutate(); }}>
      {step.captcha && <img alt="Капча hh.ru" src={step.captcha} style={{ maxWidth: "100%", borderRadius: 12 }} />}<label className={styles.field}>{step.status === "WAITING_FOR_CAPTCHA" ? "Текст с картинки" : "Код из SMS"}<input required value={code} onChange={(event) => setCode(event.target.value)} /></label><div className={styles.actionRow}><Button disabled={submit.isPending} type="submit">Подтвердить</Button>{step.status === "WAITING_FOR_CAPTCHA" && <Button className={styles.secondary} disabled={reload.isPending} type="button" onClick={() => reload.mutate()}>Обновить капчу</Button>}<Button className={styles.secondary} disabled={cancel.isPending} type="button" onClick={() => cancel.mutate()}>Отменить</Button></div>{step.message && <div className={styles.meta}>{step.message}</div>}
    </form>}
  </section>;
}

function Root() {
  const location = useLocation();
  const navigate = useNavigate();
  const dashboard = useQuery({
    queryKey: ["dashboard"],
    queryFn: async () => {
      try {
        const data = await api.dashboard();
        setCsrfToken(data.csrf_token);
        return data;
      } catch {
        await authenticate();
        const data = await api.dashboard();
        setCsrfToken(data.csrf_token);
        return data;
      }
    },
    refetchInterval: () => document.visibilityState === "visible" ? 15000 : false,
  });
  useEffect(() => {
    // Telegram opens the Mini App at the root URL. A hash router keeps manual
    // refreshes of a subsection on that same static entry point.
    const target = new URLSearchParams(window.location.search || location.search).get("target");
    if (target === "questionnaire" || target === "applications") navigate("/applications", { replace: true });
    if (target === "resume" || target === "audit") navigate("/resumes", { replace: true });
  }, [location.search, navigate]);
  if (dashboard.isPending) return <div className={styles.loading}>Загружаем кабинет…</div>;
  if (dashboard.isError || !dashboard.data) return <div className={styles.loading}>Не удалось открыть кабинет. {dashboard.error instanceof Error ? dashboard.error.message : ""}</div>;
  return <Layout dashboard={dashboard.data}><Routes><Route path="/" element={<Home dashboard={dashboard.data} />} /><Route path="/applications" element={<Applications dashboard={dashboard.data} />} /><Route path="/resumes" element={<Resumes dashboard={dashboard.data} />} /><Route path="/settings" element={<Settings dashboard={dashboard.data} />} /><Route path="*" element={<Navigate to="/" replace />} /></Routes></Layout>;
}

export default function App() { return <HashRouter><Root /></HashRouter>; }
