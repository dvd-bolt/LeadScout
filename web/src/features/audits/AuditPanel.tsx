import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { auditCategoryLabel, errorMessage, formatDate } from "../../shared/lib/format";
import type { Audit, Operation, VacancyMatchInput } from "../../shared/types/api";
import { Button, Message, type Notice } from "../../shared/ui";
import { OperationStatus, operationSucceeded } from "../../shared/ui/OperationStatus";
import styles from "../../shared/ui/UI.module.css";

const HH_VACANCY_URL = /^https:\/\/(?:[a-z0-9-]+\.)?hh\.ru\/vacancy\/\d+(?:[/?#].*)?$/i;

export function AuditPanel({ accountId, snapshotId, focus = false }: { accountId?: number; snapshotId?: number; focus?: boolean }) {
  const client = useQueryClient();
  const operationKey = `leadscout:operation:audit:${accountId ?? "independent"}`;
  const [operationId, setOperationId] = useState<string | null>(() => sessionStorage.getItem(operationKey));
  const terminalOperation = useRef<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const [auditScope, setAuditScope] = useState<"all" | "account" | "independent">("all");
  const audits = useQuery({
    queryKey: ["audits", auditScope, accountId ?? "none"],
    queryFn: () => api.audits(
      auditScope === "account" && accountId ? { accountId }
        : auditScope === "independent" ? { independentOnly: true } : {},
    ),
  });
  const activeOperations = useQuery({
    queryKey: ["operations", "audits", accountId ?? "independent"],
    queryFn: async () => [
      ...await api.operations({ kind: "resume-audit", accountId }),
      ...await api.operations({ kind: "pdf-resume-audit", accountId }),
    ],
    refetchInterval: (query) => query.state.data?.length ? 3000 : false,
  });
  const activeAudit = useMutation({
    mutationFn: () => api.createAudit({ account_id: accountId, ...(snapshotId ? { resume_snapshot_id: snapshotId } : {}) }),
    onSuccess: (data) => { terminalOperation.current = null; setNotice(null); setOperationId(data.operation_id); },
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
  });
  const handleTerminal = (operation: Operation) => {
    terminalOperation.current = operation.id;
    setOperationId(null);
    void activeOperations.refetch();
    if (operationSucceeded(operation)) {
      client.invalidateQueries({ queryKey: ["audits"] });
    }
  };
  useEffect(() => {
    if (operationId) sessionStorage.setItem(operationKey, operationId);
    else sessionStorage.removeItem(operationKey);
  }, [operationId, operationKey]);
  useEffect(() => {
    const active = activeOperations.data?.[0];
    if (active && !operationId && active.id !== terminalOperation.current) setOperationId(active.id);
  }, [activeOperations.data, operationId]);

  return <section className={`${styles.card} ${focus ? styles.highlighted : ""}`} id="audits">
    <div className={styles.cardHeader}>
      <div><h2>ИИ-аудит</h2><p className={styles.meta}>{snapshotId ? `Будет проверена выбранная версия резюме №${snapshotId}.` : "Можно проверить активное резюме или отдельный текст/PDF."}</p></div>
      {accountId ? <Button disabled={activeAudit.isPending || Boolean(operationId) || Boolean(activeOperations.data?.length)} onClick={() => activeAudit.mutate()}>{snapshotId ? "Проверить выбранное" : "Проверить активное"}</Button> : null}
    </div>
    <Message notice={audits.isError ? { text: audits.error.message, error: true } : notice} />
    <label className={styles.field}>История аудитов<select value={auditScope} onChange={(event) => setAuditScope(event.target.value as typeof auditScope)}>
      <option value="all">Все аудиты</option>
      {accountId ? <option value="account">Текущий аккаунт</option> : null}
      <option value="independent">Независимые и сохранённые после удаления аккаунта</option>
    </select></label>
    <OperationStatus operationId={operationId} scopeKey={accountId ?? "independent-audit"} onTerminal={handleTerminal} />
    <IndependentAudit accountId={accountId} operationActive={Boolean(operationId) || Boolean(activeOperations.data?.length)} onOperation={setOperationId} onError={(text) => setNotice({ text, error: true })} />
    <AuditList audits={audits.data ?? []} scopeKey={accountId ?? "independent-audit"} />
  </section>;
}

function IndependentAudit({ accountId, operationActive, onOperation, onError }: { accountId?: number; operationActive: boolean; onOperation: (id: string) => void; onError: (text: string) => void }) {
  const [text, setText] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const textAudit = useMutation({
    mutationFn: () => api.createAudit({ ...(accountId ? { account_id: accountId } : {}), resume_text: text.trim() }),
    onSuccess: (data) => onOperation(data.operation_id),
    onError: (error) => onError(errorMessage(error)),
  });
  const pdfAudit = useMutation({
    mutationFn: () => api.createPdfAudit(accountId, file!),
    onSuccess: (data) => onOperation(data.operation_id),
    onError: (error) => onError(errorMessage(error)),
  });
  const busy = operationActive || textAudit.isPending || pdfAudit.isPending;

  return <details open={!accountId}>
    <summary>Проверить отдельный текст или PDF</summary>
    <div className={styles.form} style={{ marginTop: 12 }}>
      <form className={styles.form} onSubmit={(event) => { event.preventDefault(); textAudit.mutate(); }}>
        <label className={styles.field}>Текст резюме<textarea value={text} placeholder="Вставьте текст для отдельного аудита" onChange={(event) => setText(event.target.value)} /></label>
        <Button className={styles.secondary} disabled={text.trim().length < 50 || busy} type="submit">Проверить текст</Button>
      </form>
      <form className={styles.form} onSubmit={(event) => { event.preventDefault(); pdfAudit.mutate(); }}>
        <label className={styles.field}>PDF только для аудита<input ref={fileRef} accept="application/pdf" disabled={busy} type="file" onChange={(event) => setFile(event.target.files?.[0] ?? null)} /></label>
        <Button className={styles.secondary} disabled={!file || busy} type="submit">Проверить PDF</Button>
      </form>
    </div>
  </details>;
}

function MatchSummary({ result }: { result: Record<string, unknown> }) {
  const skills = (name: string) => Array.isArray(result[name])
    ? result[name].map((item) => typeof item === "object" && item ? String((item as { value?: unknown }).value ?? "") : String(item)).filter(Boolean).join(", ")
    : "—";
  return <details className={styles.notice} open>
    <summary>Результат сравнения</summary>
    <div className={styles.matchSummary}>
      <strong>{String(result.match_score ?? "—")}/100 · {result.is_suitable ? "Можно откликаться" : "Есть критические несовпадения"}</strong>
      <div><b>Совпадает:</b> {skills("matching_skills")}</div>
      <div><b>Не хватает:</b> {skills("missing_skills")}</div>
      <div>{String(result.advice_for_apply ?? "")}</div>
    </div>
  </details>;
}

function VacancyMatchForm({ auditId, scopeKey }: { auditId: number; scopeKey: string | number }) {
  const [mode, setMode] = useState<"text" | "url">("text");
  const [value, setValue] = useState("");
  const operationKey = `leadscout:operation:match:${auditId}`;
  const [operationId, setOperationId] = useState<string | null>(() => sessionStorage.getItem(operationKey));
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const match = useMutation({
    mutationFn: (payload: VacancyMatchInput) => api.matchAudit(auditId, payload),
    onSuccess: (data) => { setResult(null); setNotice(null); setOperationId(data.operation_id); },
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
  });
  const activeMatch = useQuery({
    queryKey: ["operations", "match", auditId],
    queryFn: () => api.operations({ kind: "vacancy-match", activeOnly: false }),
    refetchInterval: (query) => query.state.data?.some((item) => ["PENDING", "RUNNING"].includes(item.status)) ? 3000 : false,
  });
  useEffect(() => {
    if (operationId) sessionStorage.setItem(operationKey, operationId);
    else sessionStorage.removeItem(operationKey);
  }, [operationId, operationKey]);
  useEffect(() => {
    const related = activeMatch.data?.filter((item) => item.resource?.startsWith(`match:${auditId}:`)) ?? [];
    const active = related.find((item) => ["PENDING", "RUNNING"].includes(item.status));
    if (active && !operationId) setOperationId(active.id);
    if (!operationId && !active && !result) {
      const completed = related.find((item) => operationSucceeded(item));
      if (completed) setResult(completed.result);
    }
  }, [activeMatch.data, auditId, operationId, result]);
  const active = activeMatch.data?.some((item) => item.resource?.startsWith(`match:${auditId}:`) && ["PENDING", "RUNNING"].includes(item.status));
  const isValid = mode === "text" ? value.trim().length >= 15 : HH_VACANCY_URL.test(value.trim());

  return <div className={styles.form}>
    <div className={styles.tabs} role="group" aria-label="Источник вакансии">
      <button className={`${styles.tab} ${mode === "text" ? styles.tabActive : ""}`} type="button" onClick={() => { setMode("text"); setValue(""); }}>Текст вакансии</button>
      <button className={`${styles.tab} ${mode === "url" ? styles.tabActive : ""}`} type="button" onClick={() => { setMode("url"); setValue(""); }}>Ссылка hh.ru</button>
    </div>
    <form className={styles.form} onSubmit={(event) => {
      event.preventDefault();
      if (!isValid) return;
      const payload: VacancyMatchInput = mode === "text" ? { vacancy_text: value.trim() } : { vacancy_url: value.trim() };
      match.mutate(payload);
    }}>
      {mode === "text" ? <label className={styles.field}>Текст вакансии<textarea value={value} placeholder="Вставьте описание вакансии" onChange={(event) => setValue(event.target.value)} /></label>
        : <label className={styles.field}>Ссылка на вакансию hh.ru<input type="url" value={value} placeholder="https://hh.ru/vacancy/123456" onChange={(event) => setValue(event.target.value)} /></label>}
      <Button className={styles.secondary} disabled={!isValid || match.isPending || Boolean(operationId) || active} type="submit">Сравнить</Button>
    </form>
    <Message notice={notice} />
    <OperationStatus operationId={operationId} scopeKey={`${scopeKey}:match:${auditId}`} onTerminal={(operation) => {
      setOperationId(null);
      if (operationSucceeded(operation)) setResult(operation.result);
      void activeMatch.refetch();
    }} />
    {result ? <MatchSummary result={result} /> : null}
  </div>;
}

function AuditList({ audits, scopeKey }: { audits: Audit[]; scopeKey: string | number }) {
  return <div className={styles.auditList}>
    {audits.length > 0 ? audits.map((audit) => <article className={styles.audit} key={audit.id}>
      <div className={styles.row}>
        <div><div className={styles.resumeTitle}>{audit.profession_name}</div><div className={styles.meta}>{audit.account_id ? `Аккаунт: ${audit.source_account_name || `#${audit.account_id}`}` : audit.source_account_name ? `Удалённый аккаунт: ${audit.source_account_name}` : "Независимый аудит"} · {formatDate(audit.created_at)}</div></div>
        <span className={styles.score}>{audit.overall_score}</span>
      </div>
      <p className={styles.meta}>{audit.summary_text}</p>
      <details><summary>Подробный отчёт</summary>
        <h3>Оценки</h3>
        <div className={styles.meta}>{Object.entries(audit.category_scores ?? {}).map(([name, score]) => <div key={name}>{auditCategoryLabel(name)}: {score}/100</div>)}</div>
        <h3>Штрафы</h3>
        {audit.penalties?.length ? <ul className={styles.recommendations}>{audit.penalties.map((item) => <li key={item}>{item}</li>)}</ul> : <p className={styles.meta}>Штрафов нет.</p>}
        <h3>Главные рекомендации</h3>
        {audit.top_recommendations?.length ? <ul className={styles.recommendations}>{audit.top_recommendations.map((item) => <li key={item}>{item}</li>)}</ul> : <p className={styles.meta}>Рекомендаций пока нет.</p>}
        <h3>Инсайты</h3>
        {audit.insights?.length ? <div className={styles.insightList}>{audit.insights.map((insight, index) => <div className={styles.insight} key={`${insight.tier}:${insight.title}:${index}`}>
          <div className={styles.row}><strong>{insight.title}</strong><span className={styles.status}>Уровень {insight.tier} · {insight.score_impact}</span></div>
          <p className={styles.meta}>{insight.description}</p>
        </div>)}</div> : <p className={styles.meta}>Инсайтов пока нет.</p>}
      </details>
      <a className={styles.textButton} href={`/api/v1/audits/${audit.id}/report`} target="_blank" rel="noreferrer">PDF-отчёт</a>
      <details><summary>Сравнить с вакансией</summary><VacancyMatchForm auditId={audit.id} scopeKey={scopeKey} /></details>
    </article>) : <div className={styles.empty}>Аудитов ещё нет.</div>}
  </div>;
}
