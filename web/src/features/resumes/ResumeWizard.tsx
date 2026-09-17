import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { ApiError } from "../../shared/http/client";
import { errorMessage, resumeStatusLabel } from "../../shared/lib/format";
import type { Operation, ResumeDraft, ResumeDraftData, ResumeFieldError } from "../../shared/types/api";
import { Button, Message, type Notice } from "../../shared/ui";
import { OperationStatus, operationSucceeded } from "../../shared/ui/OperationStatus";
import { ResumeEditorStep, SelectField, type ReferenceOption } from "./ResumeWizardFields";
import styles from "../../shared/ui/UI.module.css";

const STEPS = [
  ["profession", "Профессия"], ["personal", "Личные данные"], ["contacts", "Контакты"],
  ["conditions", "Условия"], ["skills", "Навыки"], ["experience", "Опыт"],
  ["education", "Образование"], ["languages", "Языки"], ["additional", "Дополнительно"],
  ["about", "О себе"], ["review", "Проверка"],
] as const;

type StepKey = typeof STEPS[number][0];

export function ResumeWizard({ accountId, draft, onRefresh, onSaved, onClose, onDelete, onRetryPdf }: {
  accountId: number; draft: ResumeDraft; onRefresh: () => Promise<unknown>; onSaved?: (draft: ResumeDraft) => void; onClose: () => void; onDelete: () => void;
  onRetryPdf?: (file: File) => Promise<void>;
}) {
  const initialStep = Math.max(0, STEPS.findIndex(([key]) => key === draft.current_step));
  const backupKey = `leadscout:resume-draft:${accountId}:${draft.id}`;
  const operationKey = `leadscout:operation:resume:${accountId}:${draft.id}`;
  const localBackup = (() => {
    try {
      return JSON.parse(localStorage.getItem(backupKey) || "null") as { revision?: number; data?: ResumeDraftData } | null;
    } catch { return null; }
  })();
  const restored = localBackup?.data ?? draft.data;
  const hasLocalChanges = Boolean(localBackup?.data);
  const hasInitialConflict = hasLocalChanges && localBackup?.revision !== draft.revision;
  const [data, setData] = useState<ResumeDraftData>(restored);
  const [stepIndex, setStepIndex] = useState(initialStep);
  const [dirty, setDirty] = useState(hasLocalChanges);
  const [saveState, setSaveState] = useState<"saved" | "saving" | "error">(hasInitialConflict ? "error" : hasLocalChanges ? "saving" : "saved");
  const [notice, setNotice] = useState<Notice>(null);
  const [serverConflict, setServerConflict] = useState<ResumeDraft | null>(hasInitialConflict ? draft : null);
  const [operationId, setOperationId] = useState<string | null>(() => sessionStorage.getItem(operationKey));
  const terminalOperation = useRef<string | null>(null);
  const [operationActive, setOperationActive] = useState(() => Boolean(sessionStorage.getItem(operationKey)));
  const [conflictsConfirmed, setConflictsConfirmed] = useState(false);
  const [professionOptions, setProfessionOptions] = useState<ReferenceOption[]>([]);
  const [cityOptions, setCityOptions] = useState<ReferenceOption[]>([]);
  const activeOperations = useQuery({
    queryKey: ["operations", "resume-draft", accountId, draft.id],
    queryFn: () => api.operations({ accountId, resource: String(draft.id) }),
    refetchInterval: (query) => query.state.data?.length ? 3000 : false,
  });
  const revisionRef = useRef(draft.revision);
  const dataRef = useRef(restored);
  const dirtyRef = useRef(hasLocalChanges);
  const saveRef = useRef<Promise<boolean> | null>(null);
  const step = STEPS[stepIndex][0];

  useEffect(() => {
    if (!dirtyRef.current && draft.revision >= revisionRef.current) {
      revisionRef.current = draft.revision;
      dataRef.current = draft.data;
      setData(draft.data);
      setSaveState("saved");
    }
  }, [draft.data, draft.revision]);

  const mutateData = useCallback((mutator: (next: ResumeDraftData) => void) => {
    const next = structuredClone(dataRef.current);
    mutator(next);
    dataRef.current = next;
    dirtyRef.current = true;
    setConflictsConfirmed(false);
    localStorage.setItem(backupKey, JSON.stringify({ revision: revisionRef.current, data: next }));
    setDirty(true);
    setData(next);
    setSaveState("saving");
    setNotice(null);
  }, [backupKey]);

  const save = useCallback(async (targetStep: StepKey = STEPS[stepIndex][0], force = false): Promise<boolean> => {
    if (saveRef.current) await saveRef.current;
    if (!dirtyRef.current && !force) return true;
    const snapshot = dataRef.current;
    const signature = JSON.stringify(snapshot);
    setSaveState("saving");
    const promise = api.patchResumeDraft(accountId, draft.id, revisionRef.current, targetStep, snapshot)
      .then((updated) => {
        revisionRef.current = updated.revision;
        onSaved?.(updated);
        if (JSON.stringify(dataRef.current) === signature) {
          dirtyRef.current = false;
          setDirty(false);
          setSaveState("saved");
          localStorage.removeItem(backupKey);
        }
        return true;
      })
      .catch(async (error) => {
        setSaveState("error");
        if (error instanceof ApiError && error.status === 409) {
          try {
            const latest = await api.resumeDraft(accountId, draft.id);
            setServerConflict(latest);
            localStorage.setItem(backupKey, JSON.stringify({ revision: revisionRef.current, data: dataRef.current }));
            setNotice({ text: "Черновик изменён в другой вкладке. Выберите, какую версию сохранить.", error: true });
          } catch (refreshError) {
            setNotice({ text: errorMessage(refreshError), error: true });
          }
        } else {
          setNotice({ text: errorMessage(error), error: true });
        }
        return false;
      })
      .finally(() => { saveRef.current = null; });
    saveRef.current = promise;
    return promise;
  }, [accountId, backupKey, draft.id, onSaved, stepIndex]);

  useEffect(() => setConflictsConfirmed(false), [draft.preflight_fingerprint, draft.revision]);
  useEffect(() => {
    if (operationId) sessionStorage.setItem(operationKey, operationId);
    else sessionStorage.removeItem(operationKey);
  }, [operationId, operationKey]);
  useEffect(() => {
    const active = activeOperations.data?.find((item) => item.kind.startsWith("resume-draft-"));
    if (active && !operationId && active.id !== terminalOperation.current) {
      setOperationId(active.id);
      setOperationActive(true);
    }
  }, [activeOperations.data, operationId]);

  useEffect(() => {
    if (!dirty || serverConflict) return undefined;
    const timer = window.setTimeout(() => { void save(); }, 700);
    return () => window.clearTimeout(timer);
  }, [data, dirty, save, serverConflict, stepIndex]);

  const errors = useMemo(() => new Map(
    (draft.validation?.field_errors || []).map((item: ResumeFieldError) => [item.path, item.message]),
  ), [draft.validation]);

  const operation = useMutation({
    mutationFn: async (kind: "preflight" | "publish" | "resume" | "reconcile") => {
      if (!await save("review")) throw new Error("Сначала сохраните черновик.");
      if (kind === "preflight") return api.preflightResumeDraft(accountId, draft.id);
      if (kind === "resume") return api.resumeResumeDraft(
        accountId, draft.id, revisionRef.current,
        conflictsConfirmed ? draft.preflight_fingerprint : "",
      );
      if (kind === "reconcile") return api.reconcileResumeDraft(accountId, draft.id);
      const key = `${draft.id}-${revisionRef.current}-${crypto.randomUUID()}`;
      return api.publishResumeDraft(
        accountId, draft.id, revisionRef.current, key,
        conflictsConfirmed ? draft.preflight_fingerprint : "",
      );
    },
    onSuccess: (result) => { terminalOperation.current = null; setNotice(null); setOperationId(result.operation_id); setOperationActive(true); },
    onError: async (error) => {
      await onRefresh();
      setNotice({ text: `${errorMessage(error)} Черновик обновлён; используйте доступное продолжение или сверку.`, error: true });
    },
  });
  const validate = useMutation({
    mutationFn: async () => {
      if (!await save("review")) throw new Error("Черновик не сохранён.");
      return api.validateResumeDraft(accountId, draft.id);
    },
    onSuccess: async (result) => {
      await onRefresh();
      setNotice(result.valid
        ? { text: "Данные заполнены. Теперь сравните их с профилем hh.ru.", tone: "success" }
        : { text: `Нужно исправить полей: ${result.field_errors.length}.`, error: true });
    },
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
  });

  const go = async (next: number) => {
    if (serverConflict) return;
    const bounded = Math.max(0, Math.min(STEPS.length - 1, next));
    if (bounded > stepIndex && !await save(STEPS[bounded][0], true)) return;
    setStepIndex(bounded);
  };

  const close = async () => {
    if (serverConflict) { onClose(); return; }
    if (!dirtyRef.current || await save(step, true)) onClose();
  };

  const loadServerVersion = () => {
    if (!serverConflict) return;
    revisionRef.current = serverConflict.revision;
    dataRef.current = serverConflict.data;
    setData(serverConflict.data);
    dirtyRef.current = false;
    setDirty(false);
    setSaveState("saved");
    localStorage.removeItem(backupKey);
    onSaved?.(serverConflict);
    setServerConflict(null);
    setNotice({ text: "Загружена актуальная версия с сервера.", tone: "success" });
  };

  const overwriteServerVersion = async () => {
    if (!serverConflict) return;
    revisionRef.current = serverConflict.revision;
    setServerConflict(null);
    if (await save(step, true)) {
      setNotice({ text: "Локальные изменения сохранены поверх серверной версии.", tone: "success" });
    }
  };

  const terminal = async (item: Operation) => {
    terminalOperation.current = item.id;
    setOperationActive(false);
    setOperationId(null);
    void activeOperations.refetch();
    await onRefresh();
    const message = typeof item.result.message === "string" ? item.result.message : item.error_text;
    const options = (item.result as Record<string, unknown>).options;
    if (Array.isArray(options)) {
      const normalized = options.flatMap((value: unknown) => {
        if (typeof value === "string") return [{ id: value, label: value }];
        if (value && typeof value === "object") {
          const option = value as Record<string, unknown>;
          const label = String(option.label ?? "").trim();
          if (label) return [{ id: String(option.id ?? label), label }];
        }
        return [];
      });
      if ((item.result as Record<string, unknown>).code === "AMBIGUOUS_CITY") setCityOptions(normalized);
      else setProfessionOptions(normalized);
    }
    setNotice(message ? { text: message, error: !operationSucceeded(item), tone: operationSucceeded(item) ? "success" : "warning" } : null);
  };

  const busy = operation.isPending || validate.isPending || operationActive || saveState === "saving";
  const canResumeAttempt = ["NEEDS_ACTION", "PARTIAL"].includes(draft.latest_publish_status || "");
  const canReconcileAttempt = draft.latest_publish_status === "UNCERTAIN" || Boolean(
    draft.hh_resume_id && ["FAILED", "NEEDS_REVIEW"].includes(draft.status),
  );
  const extractionWarnings = draft.validation?.extraction_warnings || [];
  const ordinaryFieldErrors = (draft.validation?.field_errors || []).filter(
    (item) => item.code !== "PDF_SECTION_MISSING",
  );

  if (draft.status === "COMPLETED") {
    return <div className={styles.wizard}>
      <div className={styles.cardHeader}>
        <div><h3>{draft.data.profession.title || "Завершённое резюме"}</h3><div className={styles.meta}>Перенос подтверждён · статус hh.ru: {draft.hh_status || "не указан"}</div></div>
        <button className={styles.textButton} onClick={() => void close()}>Закрыть</button>
      </div>
      <div className={styles.reviewGrid}>
        <div>{draft.data.personal.last_name} {draft.data.personal.first_name} · {draft.data.personal.city}</div>
        <div className={styles.meta}>{draft.data.experiences.filter((item) => item.selected).length} мест работы · {draft.data.education.filter((item) => item.selected).length} образований · {draft.data.skills.length} навыков</div>
        {draft.hh_resume_url ? <a href={draft.hh_resume_url} target="_blank" rel="noreferrer">Открыть резюме на hh.ru</a> : null}
      </div>
      <p className={styles.meta}>Завершённый черновик доступен только для просмотра. Для нового резюме начните отдельное создание.</p>
      <div className={styles.dangerZone}><button className={`${styles.textButton} ${styles.danger}`} onClick={onDelete}>Удалить локальный черновик</button></div>
    </div>;
  }

  return <div className={styles.wizard}>
    <div className={styles.cardHeader}>
      <div><h3>{data.profession.title || "Новый черновик"}</h3><div className={styles.meta}>Статус: {resumeStatusLabel(draft.status)} · версия {revisionRef.current}</div></div>
      <button className={styles.textButton} onClick={() => void close()}>Закрыть</button>
    </div>
    <div className={styles.stepTabs} aria-label="Шаги резюме">
      {STEPS.map(([key, label], index) => <button
        type="button" key={key} aria-current={index === stepIndex ? "step" : undefined}
        className={`${styles.stepTab} ${index === stepIndex ? styles.stepTabActive : ""}`}
        onClick={() => void go(index)}>{index + 1}. {label}</button>)}
    </div>
    <div className={styles.saveStatus} role="status">
      {saveState === "saving" ? "Сохраняю…" : saveState === "error" ? "Не сохранено" : "Все изменения сохранены"}
    </div>
    <Message notice={notice} />
    {serverConflict ? <div className={`${styles.notice} ${styles.warningNotice}`}>
      <div>
        <strong>Конфликт версий</strong>
        <p className={styles.meta}>На сервере версия {serverConflict.revision}; локальные изменения сохранены в этом браузере.</p>
        <div className={styles.actionRow}>
          <Button className={styles.secondary} onClick={loadServerVersion}>Загрузить серверную</Button>
          <Button onClick={() => void overwriteServerVersion()}>Сохранить локальную</Button>
        </div>
      </div>
    </div> : null}
    <OperationStatus operationId={operationId} scopeKey={`${accountId}:${draft.id}`} onTerminal={(item) => void terminal(item)} onError={() => setOperationActive(false)} />

    <div className={styles.wizardPanel}>
      <ResumeEditorStep
        step={step}
        data={data}
        draft={draft}
        errors={errors}
        backupKey={backupKey}
        professionOptions={professionOptions}
        cityOptions={cityOptions}
        mutateData={mutateData}
      />

      {step === "review" ? <>
        <div className={styles.reviewGrid}>
          <div><strong>{data.profession.title || "Без названия"}</strong><div className={styles.meta}>{data.personal.last_name} {data.personal.first_name} · {data.personal.city || "город не указан"}</div></div>
          <div className={styles.meta}>{data.experiences.filter((item) => item.selected).length} мест работы · {data.education.filter((item) => item.selected).length} образований · {data.skills.length} навыков</div>
        </div>
        {extractionWarnings.length ? <div className={`${styles.notice} ${styles.error}`}>
          <div><strong>PDF распознан не полностью</strong><ul>{extractionWarnings.map((item) => <li key={`${item.path}-${item.code}`}>{item.message}</li>)}</ul>
            {onRetryPdf ? <label aria-disabled={busy} className={`${styles.button} ${styles.secondary} ${styles.fileAction}`}>
              Повторить распознавание в этом черновике
              <input disabled={busy} aria-label="Повторить распознавание PDF в текущем черновике" type="file" accept="application/pdf" onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void onRetryPdf(file);
                event.target.value = "";
              }} />
            </label> : null}
          </div>
        </div> : null}
        {ordinaryFieldErrors.length ? <div className={`${styles.notice} ${styles.error}`}>
          <div><strong>Исправьте данные перед публикацией</strong><ul>{ordinaryFieldErrors.map((item) => <li key={`${item.path}-${item.code}`}>{item.message}</li>)}</ul></div>
        </div> : null}
        {onRetryPdf && !extractionWarnings.length ? <label aria-disabled={busy} className={`${styles.button} ${styles.secondary} ${styles.fileAction}`}>
          Повторно распознать PDF в этом черновике
          <input disabled={busy} aria-label="Повторно распознать PDF в текущем черновике" type="file" accept="application/pdf" onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void onRetryPdf(file);
            event.target.value = "";
          }} />
        </label> : null}
        {draft.preflight.conflicts?.length ? <div className={`${styles.notice} ${styles.warningNotice}`}>
          <div><strong>Конфликты с профилем hh.ru</strong><ul>{draft.preflight.conflicts.map((item) => <li key={item.path}>{item.path}: «{item.profile_value}» → «{item.draft_value}»</li>)}</ul>
            <label className={styles.switch}><input type="checkbox" checked={conflictsConfirmed} onChange={(event) => setConflictsConfirmed(event.target.checked)} />Я подтверждаю эти изменения для текущей версии черновика</label>
          </div>
        </div> : null}
        <label className={styles.switch}><input type="checkbox" checked={data.publication.target_account_confirmed} onChange={(event) => mutateData((next) => { next.publication.target_account_confirmed = event.target.checked; })} />Я проверил целевой аккаунт hh.ru</label>
        <SelectField label="Видимость резюме" value={data.publication.visibility} options={[["Виден всем работодателям", "Всем работодателям"], ["Виден только зарегистрированным работодателям", "Только зарегистрированным"], ["Не виден никому", "Никому"]]} onChange={(value) => mutateData((next) => { next.publication.visibility = value; })} />
        <div className={styles.actionRow}>
          <Button disabled={busy} className={styles.secondary} onClick={() => validate.mutate()}>Проверить поля</Button>
          <Button disabled={busy} className={styles.secondary} onClick={() => operation.mutate("preflight")}>Сравнить с hh.ru</Button>
          <Button disabled={busy || dirty || canResumeAttempt || !data.publication.target_account_confirmed || draft.preflight_revision !== revisionRef.current || Boolean(draft.preflight.conflicts?.length && !conflictsConfirmed)} onClick={() => operation.mutate("publish")}>Создать на hh.ru</Button>
        </div>
        {canResumeAttempt ? <Button disabled={busy || dirty || !data.publication.target_account_confirmed || draft.preflight_revision !== revisionRef.current || Boolean(draft.preflight.conflicts?.length && !conflictsConfirmed)} onClick={() => operation.mutate("resume")}>{draft.hh_resume_id ? "Продолжить перенос в созданное резюме" : "Повторить публикацию сохранённого черновика"}</Button> : null}
        {canReconcileAttempt ? <Button disabled={busy} className={styles.secondary} onClick={() => operation.mutate("reconcile")}>Сверить результат на hh.ru</Button> : null}
        {draft.hh_resume_url ? <a href={draft.hh_resume_url} target="_blank" rel="noreferrer">Открыть созданное резюме на hh.ru</a> : null}
      </> : null}
    </div>

    <div className={styles.wizardFooter}>
      <Button className={styles.secondary} disabled={stepIndex === 0 || busy} onClick={() => void go(stepIndex - 1)}>Назад</Button>
      {stepIndex < STEPS.length - 1 ? <Button disabled={busy} onClick={() => void go(stepIndex + 1)}>Сохранить и продолжить</Button> : null}
    </div>
    <div className={styles.dangerZone}><button className={`${styles.textButton} ${styles.danger}`} onClick={onDelete}>Удалить локальный черновик</button></div>
  </div>;
}
