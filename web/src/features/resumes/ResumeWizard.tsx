import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { errorMessage } from "../../shared/lib/format";
import type { NamedResumeDetail, Operation, ResumeDraft, ResumeDraftData, ResumeFieldError } from "../../shared/types/api";
import { Button, Message, type Notice } from "../../shared/ui";
import { OperationStatus, operationSucceeded } from "../../shared/ui/OperationStatus";
import styles from "../../shared/ui/UI.module.css";

const STEPS = [
  ["profession", "Профессия"], ["personal", "Личные данные"], ["contacts", "Контакты"],
  ["conditions", "Условия"], ["skills", "Навыки"], ["experience", "Опыт"],
  ["education", "Образование"], ["languages", "Языки"], ["additional", "Дополнительно"],
  ["about", "О себе"], ["review", "Проверка"],
] as const;

type StepKey = typeof STEPS[number][0];

function csv(value: string): string[] {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function lines(value: string): string[] {
  return value.split("\n").map((item) => item.trim()).filter(Boolean);
}

function namedDetails(value: string): NamedResumeDetail[] {
  return lines(value).map((name) => ({ name, organization: "", year: "", description: "" }));
}

function TextField({ label, value, onChange, type = "text", error }: {
  label: string; value: string | number; onChange: (value: string) => void; type?: string; error?: string;
}) {
  return <label className={styles.field}>{label}
    <input aria-invalid={Boolean(error)} type={type} value={value} onChange={(event) => onChange(event.target.value)} />
    {error ? <span className={styles.fieldError}>{error}</span> : null}
  </label>;
}

function TextArea({ label, value, onChange, hint }: { label: string; value: string; onChange: (value: string) => void; hint?: string }) {
  return <label className={styles.field}>{label}<textarea value={value} onChange={(event) => onChange(event.target.value)} />
    {hint ? <span className={styles.meta}>{hint}</span> : null}
  </label>;
}

export function ResumeWizard({ accountId, draft, onRefresh, onSaved, onClose, onDelete, onRetryPdf }: {
  accountId: number; draft: ResumeDraft; onRefresh: () => Promise<unknown>; onSaved?: (draft: ResumeDraft) => void; onClose: () => void; onDelete: () => void;
  onRetryPdf?: (file: File) => Promise<void>;
}) {
  const initialStep = Math.max(0, STEPS.findIndex(([key]) => key === draft.current_step));
  const [data, setData] = useState<ResumeDraftData>(draft.data);
  const [stepIndex, setStepIndex] = useState(initialStep);
  const [dirty, setDirty] = useState(false);
  const [saveState, setSaveState] = useState<"saved" | "saving" | "error">("saved");
  const [notice, setNotice] = useState<Notice>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const [operationActive, setOperationActive] = useState(false);
  const [conflictsConfirmed, setConflictsConfirmed] = useState(false);
  const revisionRef = useRef(draft.revision);
  const dataRef = useRef(draft.data);
  const dirtyRef = useRef(false);
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
    setDirty(true);
    setData(next);
    setSaveState("saving");
    setNotice(null);
  }, []);

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
        }
        return true;
      })
      .catch((error) => {
        setSaveState("error");
        setNotice({ text: errorMessage(error), error: true });
        return false;
      })
      .finally(() => { saveRef.current = null; });
    saveRef.current = promise;
    return promise;
  }, [accountId, draft.id, onSaved, stepIndex]);

  useEffect(() => {
    if (!dirty) return undefined;
    const timer = window.setTimeout(() => { void save(); }, 700);
    return () => window.clearTimeout(timer);
  }, [data, dirty, save, stepIndex]);

  const errors = useMemo(() => new Map(
    (draft.validation?.field_errors || []).map((item: ResumeFieldError) => [item.path, item.message]),
  ), [draft.validation]);

  const operation = useMutation({
    mutationFn: async (kind: "preflight" | "publish" | "resume" | "reconcile") => {
      if (!await save("review")) throw new Error("Сначала сохраните черновик.");
      if (kind === "preflight") return api.preflightResumeDraft(accountId, draft.id);
      if (kind === "resume") return api.resumeResumeDraft(accountId, draft.id);
      if (kind === "reconcile") return api.reconcileResumeDraft(accountId, draft.id);
      const key = `${draft.id}-${revisionRef.current}-${crypto.randomUUID()}`;
      return api.publishResumeDraft(
        accountId, draft.id, revisionRef.current, key,
        conflictsConfirmed ? draft.preflight_fingerprint : "",
      );
    },
    onSuccess: (result) => { setNotice(null); setOperationId(result.operation_id); setOperationActive(true); },
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
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
    const bounded = Math.max(0, Math.min(STEPS.length - 1, next));
    if (bounded > stepIndex && !await save(STEPS[bounded][0], true)) return;
    setStepIndex(bounded);
  };

  const terminal = async (item: Operation) => {
    setOperationActive(false);
    await onRefresh();
    const message = typeof item.result.message === "string" ? item.result.message : item.error_text;
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
        <button className={styles.textButton} onClick={onClose}>Закрыть</button>
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
      <div><h3>{data.profession.title || "Новый черновик"}</h3><div className={styles.meta}>Статус: {draft.status} · версия {revisionRef.current}</div></div>
      <button className={styles.textButton} onClick={onClose}>Закрыть</button>
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
    <OperationStatus operationId={operationId} scopeKey={`${accountId}:${draft.id}`} onTerminal={(item) => void terminal(item)} onError={() => setOperationActive(false)} />

    <div className={styles.wizardPanel}>
      {step === "profession" ? <>
        <TextField label="Название резюме" value={data.profession.title} error={errors.get("profession.title")} onChange={(value) => mutateData((next) => { next.profession.title = value; })} />
        <TextField label="Профессия hh.ru" value={data.profession.hh_profession} error={errors.get("profession.hh_profession")} onChange={(value) => mutateData((next) => { next.profession.hh_profession = value; })} />
        <TextField label="ID профессии hh.ru (после подтверждения)" value={data.profession.hh_profession_id} onChange={(value) => mutateData((next) => { next.profession.hh_profession_id = value; })} />
        <TextField label="Специализации, через запятую" value={data.profession.specializations.join(", ")} onChange={(value) => mutateData((next) => { next.profession.specializations = csv(value); })} />
      </> : null}

      {step === "personal" ? <>
        <div className={styles.fieldGrid}>
          {(["last_name", "first_name", "middle_name"] as const).map((key) => <TextField key={key}
            label={{ last_name: "Фамилия", first_name: "Имя", middle_name: "Отчество" }[key]}
            value={data.personal[key]} error={errors.get(`personal.${key}`)}
            onChange={(value) => mutateData((next) => { next.personal[key] = value; })} />)}
          <TextField label="Дата рождения" type="date" value={data.personal.birth_date} error={errors.get("personal.birth_date")} onChange={(value) => mutateData((next) => { next.personal.birth_date = value; })} />
          <TextField label="Пол" value={data.personal.gender} onChange={(value) => mutateData((next) => { next.personal.gender = value; })} />
          <TextField label="Город" value={data.personal.city} error={errors.get("personal.city")} onChange={(value) => mutateData((next) => { next.personal.city = value; })} />
        </div>
        <TextField label="Гражданство, через запятую" value={data.personal.citizenships.join(", ")} onChange={(value) => mutateData((next) => { next.personal.citizenships = csv(value); })} />
        <TextField label="Разрешение на работу, через запятую" value={data.personal.work_authorizations.join(", ")} onChange={(value) => mutateData((next) => { next.personal.work_authorizations = csv(value); })} />
      </> : null}

      {step === "contacts" ? <div className={styles.fieldGrid}>
        <TextField label="Телефон" value={data.contacts.phone} onChange={(value) => mutateData((next) => { next.contacts.phone = value; })} />
        <TextField label="Email" type="email" value={data.contacts.email} onChange={(value) => mutateData((next) => { next.contacts.email = value; })} />
        <TextField label="Telegram" value={data.contacts.telegram} onChange={(value) => mutateData((next) => { next.contacts.telegram = value; })} />
        <TextField label="Предпочтительный контакт" value={data.contacts.preferred} onChange={(value) => mutateData((next) => { next.contacts.preferred = value; })} />
        <TextField label="Способы связи, через запятую" value={data.contacts.methods.join(", ")} onChange={(value) => mutateData((next) => { next.contacts.methods = csv(value); })} />
      </div> : null}

      {step === "conditions" ? <div className={styles.fieldGrid}>
        <TextField label="Зарплата" type="number" value={data.work_conditions.salary ?? ""} onChange={(value) => mutateData((next) => { next.work_conditions.salary = value ? Number(value) : null; })} />
        <TextField label="Валюта" value={data.work_conditions.currency} onChange={(value) => mutateData((next) => { next.work_conditions.currency = value; })} />
        <TextField label="Занятость, через запятую" value={data.work_conditions.employment_types.join(", ")} onChange={(value) => mutateData((next) => { next.work_conditions.employment_types = csv(value); })} />
        <TextField label="График, через запятую" value={data.work_conditions.schedules.join(", ")} onChange={(value) => mutateData((next) => { next.work_conditions.schedules = csv(value); })} />
        <TextField label="Формат работы, через запятую" value={data.work_conditions.work_formats.join(", ")} onChange={(value) => mutateData((next) => { next.work_conditions.work_formats = csv(value); })} />
        <TextField label="Переезд" value={data.work_conditions.relocation} onChange={(value) => mutateData((next) => { next.work_conditions.relocation = value; })} />
        <TextField label="Командировки" value={data.work_conditions.business_trips} onChange={(value) => mutateData((next) => { next.work_conditions.business_trips = value; })} />
      </div> : null}

      {step === "skills" ? <>
        <TextArea label="Навыки" hint="Один навык на строку. Уровень можно указать через |, например: Python | продвинутый. Ничего не обрезается молча."
          value={data.skills.map((item) => `${item.name}${item.level ? ` | ${item.level}` : ""}`).join("\n")}
          onChange={(value) => mutateData((next) => { next.skills = lines(value).map((row) => { const [name, level = ""] = row.split("|").map((item) => item.trim()); return { name, level }; }); })} />
        <div className={styles.meta}>Сейчас навыков: {data.skills.length}{draft.preflight.capabilities?.max_skills ? ` · лимит hh: ${draft.preflight.capabilities.max_skills}` : ""}</div>
      </> : null}

      {step === "experience" ? <>
        {data.experiences.map((item, index) => <article className={styles.repeatCard} key={`experience-${index}`}>
          <div className={styles.cardHeader}><h3>Место работы {index + 1}</h3><button className={styles.textButton} onClick={() => mutateData((next) => { next.experiences.splice(index, 1); })}>Удалить</button></div>
          <label className={styles.switch}><input type="checkbox" checked={item.selected} onChange={(event) => mutateData((next) => { next.experiences[index].selected = event.target.checked; })} />Переносить в резюме</label>
          <div className={styles.fieldGrid}>
            {(["company", "position", "city", "start_month", "start_year", "end_month", "end_year"] as const).map((key) => <TextField key={key}
              label={{ company: "Компания", position: "Должность", city: "Город", start_month: "Месяц начала", start_year: "Год начала", end_month: "Месяц окончания", end_year: "Год окончания" }[key]}
              value={item[key]} error={errors.get(`experiences.${index}.${key}`)}
              onChange={(value) => mutateData((next) => { next.experiences[index][key] = value; })} />)}
          </div>
          <label className={styles.switch}><input type="checkbox" checked={item.is_current} onChange={(event) => mutateData((next) => { next.experiences[index].is_current = event.target.checked; })} />Работаю сейчас</label>
          <TextArea label="Обязанности и достижения" value={item.description} onChange={(value) => mutateData((next) => { next.experiences[index].description = value; })} />
        </article>)}
        <Button className={styles.secondary} onClick={() => mutateData((next) => { next.experiences.push({ company: "", position: "", city: "", start_month: "", start_year: "", is_current: false, end_month: "", end_year: "", description: "", selected: true }); })}>Добавить место работы</Button>
      </> : null}

      {step === "education" ? <>
        {data.education.map((item, index) => <article className={styles.repeatCard} key={`education-${index}`}>
          <div className={styles.cardHeader}><h3>Образование {index + 1}</h3><button className={styles.textButton} onClick={() => mutateData((next) => { next.education.splice(index, 1); })}>Удалить</button></div>
          <label className={styles.switch}><input type="checkbox" checked={item.selected} onChange={(event) => mutateData((next) => { next.education[index].selected = event.target.checked; })} />Переносить в резюме</label>
          <div className={styles.fieldGrid}>
            {(["level", "institution", "faculty", "specialization", "end_year"] as const).map((key) => <TextField key={key}
              label={{ level: "Уровень", institution: "Учебное заведение", faculty: "Факультет", specialization: "Специальность", end_year: "Год окончания" }[key]}
              value={item[key]} error={errors.get(`education.${index}.${key}`)}
              onChange={(value) => mutateData((next) => { next.education[index][key] = value; })} />)}
          </div>
        </article>)}
        <Button className={styles.secondary} onClick={() => mutateData((next) => { next.education.push({ level: "", institution: "", faculty: "", specialization: "", end_year: "", selected: true }); })}>Добавить образование</Button>
      </> : null}

      {step === "languages" ? <>
        {data.languages.map((item, index) => <div className={styles.inlineFields} key={`language-${index}`}>
          <TextField label="Язык" value={item.name} onChange={(value) => mutateData((next) => { next.languages[index].name = value; })} />
          <TextField label="Уровень" value={item.level} onChange={(value) => mutateData((next) => { next.languages[index].level = value; })} />
          <button className={styles.textButton} onClick={() => mutateData((next) => { next.languages.splice(index, 1); })}>Удалить</button>
        </div>)}
        <Button className={styles.secondary} onClick={() => mutateData((next) => { next.languages.push({ name: "", level: "" }); })}>Добавить язык</Button>
      </> : null}

      {step === "additional" ? <>
        {(["courses", "exams", "certificates", "recommendations"] as const).map((key) => <TextArea key={key}
          label={{ courses: "Курсы", exams: "Экзамены", certificates: "Сертификаты", recommendations: "Рекомендации" }[key]}
          hint="Одна запись на строку. Фото и файлы не загружаются."
          value={data.additional[key].map((item) => item.name).join("\n")}
          onChange={(value) => mutateData((next) => { next.additional[key] = namedDetails(value); })} />)}
        <TextField label="Категории водительских прав, через запятую" value={data.additional.driving_licenses.join(", ")} onChange={(value) => mutateData((next) => { next.additional.driving_licenses = csv(value); })} />
        <label className={styles.switch}><input type="checkbox" checked={data.additional.has_car} onChange={(event) => mutateData((next) => { next.additional.has_car = event.target.checked; })} />Есть автомобиль</label>
      </> : null}

      {step === "about" ? <>
        <TextArea label="О себе" value={data.about.text} onChange={(value) => mutateData((next) => { next.about.text = value; })} />
        {data.about.links.map((item, index) => <div className={styles.inlineFields} key={`link-${index}`}>
          <TextField label="Название ссылки" value={item.label} onChange={(value) => mutateData((next) => { next.about.links[index].label = value; })} />
          <TextField label="URL" type="url" value={item.url} onChange={(value) => mutateData((next) => { next.about.links[index].url = value; })} />
          <button className={styles.textButton} onClick={() => mutateData((next) => { next.about.links.splice(index, 1); })}>Удалить</button>
        </div>)}
        <Button className={styles.secondary} onClick={() => mutateData((next) => { next.about.links.push({ label: "", url: "" }); })}>Добавить ссылку</Button>
      </> : null}

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
        <TextField label="Видимость резюме" value={data.publication.visibility} onChange={(value) => mutateData((next) => { next.publication.visibility = value; })} />
        <div className={styles.actionRow}>
          <Button disabled={busy} className={styles.secondary} onClick={() => validate.mutate()}>Проверить поля</Button>
          <Button disabled={busy} className={styles.secondary} onClick={() => operation.mutate("preflight")}>Сравнить с hh.ru</Button>
          <Button disabled={busy || dirty || canResumeAttempt || !data.publication.target_account_confirmed || draft.preflight_revision !== revisionRef.current || Boolean(draft.preflight.conflicts?.length && !conflictsConfirmed)} onClick={() => operation.mutate("publish")}>Создать на hh.ru</Button>
        </div>
        {canResumeAttempt ? <Button disabled={busy} onClick={() => operation.mutate("resume")}>{draft.hh_resume_id ? "Продолжить перенос в созданное резюме" : "Повторить публикацию сохранённого черновика"}</Button> : null}
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
