import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { ApiError } from "../../shared/http/client";
import { errorMessage } from "../../shared/lib/format";
import type { Questionnaire, QuestionnaireAnswer } from "../../shared/types/api";
import { Button, EmptyState, Message, type Notice } from "../../shared/ui";
import styles from "../../shared/ui/UI.module.css";

type QuestionnaireAction = {
  kind: "save" | "confirm" | "skip";
  item: Questionnaire;
  values?: { cover_letter?: string; answers?: QuestionnaireAnswer[] };
};
type QuestionnaireConflict = {
  server: Questionnaire;
  localLetter: string;
  localAnswers: QuestionnaireAnswer[];
};

const FINISHED = new Set(["SUBMITTED", "SKIPPED"]);
const REVIEW_LOCKED = new Set(["NEEDS_REVIEW"]);

function cacheQuestionnaire(client: QueryClient, item: Questionnaire) {
  client.setQueryData<Questionnaire>(["questionnaire", item.account_id, item.id], item);
  client.setQueryData<Questionnaire>(["questionnaire-direct", item.id], item);
  client.setQueryData<Questionnaire[]>(["questionnaires", item.account_id], (current) => {
    if (!current) return current;
    const exists = current.some((candidate) => candidate.id === item.id);
    return exists ? current.map((candidate) => candidate.id === item.id ? item : candidate) : [item, ...current];
  });
}

async function refreshRelated(client: QueryClient, item: Questionnaire) {
  await Promise.all([
    client.invalidateQueries({ queryKey: ["questionnaire", item.account_id, item.id] }),
    client.invalidateQueries({ queryKey: ["questionnaire-direct", item.id] }),
    client.invalidateQueries({ queryKey: ["questionnaires", item.account_id] }),
    client.invalidateQueries({ queryKey: ["applications", item.account_id] }),
    client.invalidateQueries({ queryKey: ["dashboard"] }),
  ]);
}

function statusText(status: string) {
  switch (status) {
    case "PENDING": return "Ожидает решения";
    case "NEEDS_REVIEW": return "Нужна проверка";
    case "SUBMITTING": return "Отправляется";
    case "SUBMITTED": return "Отправлена";
    case "SKIPPED": return "Пропущена";
    case "FAILED": return "Ошибка отправки";
    default: return `Состояние: ${status || "неизвестно"}`;
  }
}

export function QuestionnaireList({ items, showProcessed = false }: { items: Questionnaire[]; showProcessed?: boolean }) {
  const client = useQueryClient();
  const [notice, setNotice] = useState<Notice>(null);
  const [conflicts, setConflicts] = useState<Record<number, QuestionnaireConflict>>({});
  const [serverReset, setServerReset] = useState<Record<number, number>>({});
  const previousStatuses = useRef(new Map<number, string>());
  useEffect(() => {
    const previous = previousStatuses.current;
    previousStatuses.current = new Map(items.map((item) => [item.id, item.status]));
    for (const [id, status] of previous) {
      if (status === "SUBMITTING" && !items.some((item) => item.id === id)) {
        // Completed questionnaires disappear from the review list response.
        void api.questionnaire(id).then((item) => {
          cacheQuestionnaire(client, item);
          return refreshRelated(client, item);
        }).catch(() => {
          void client.invalidateQueries({ queryKey: ["dashboard"] });
          void client.invalidateQueries({ queryKey: ["applications"] });
        });
      }
    }
    for (const item of items) {
      if (previous.get(item.id) === "SUBMITTING" && item.status !== "SUBMITTING") {
        cacheQuestionnaire(client, item);
        void refreshRelated(client, item);
      }
    }
  }, [client, items]);
  const action = useMutation({
    onMutate: async ({ item }: QuestionnaireAction) => {
      setNotice(null);
      await Promise.all([
        client.cancelQueries({ queryKey: ["questionnaire", item.account_id, item.id] }),
        client.cancelQueries({ queryKey: ["questionnaire-direct", item.id] }),
        client.cancelQueries({ queryKey: ["questionnaires", item.account_id] }),
      ]);
    },
    mutationFn: async ({ kind, item, values }: QuestionnaireAction) => {
      if (kind === "skip") return api.skipQuestionnaire(item.id);
      const updated = await api.updateQuestionnaire(item.id, {
        expected_revision: item.revision,
        ...(values ?? {}),
      });
      if (kind === "confirm") {
        cacheQuestionnaire(client, { ...updated, status: "SUBMITTING" });
        await api.confirmQuestionnaire(item.id, updated.revision);
        return api.questionnaire(item.id);
      }
      return updated;
    },
    onSuccess: async (updated, variables) => {
      setConflicts((current) => {
        const next = { ...current };
        delete next[variables.item.id];
        return next;
      });
      cacheQuestionnaire(client, updated);
      await refreshRelated(client, updated);
      setNotice({ text: variables.kind === "confirm" ? "Анкета передана на отправку" : variables.kind === "skip" ? "Анкета пропущена" : "Анкета сохранена" });
    },
    onError: async (error, variables) => {
      try {
        const current = await api.questionnaire(variables.item.id);
        cacheQuestionnaire(client, current);
        await refreshRelated(client, current);
        if (error instanceof ApiError && error.status === 409) {
          const localLetter = variables.values?.cover_letter ?? variables.item.cover_letter;
          setConflicts((existing) => ({
            ...existing,
            [variables.item.id]: {
              server: current,
              localLetter,
              localAnswers: variables.values?.answers ?? variables.item.ai_payload.answers ?? [],
            },
          }));
          setNotice({ text: `Анкета уже изменилась. Актуальное состояние: ${statusText(current.status)}. Ваши ответы сохранены ниже для сравнения.`, error: true });
          return;
        }
      } catch {
        // A lost response may follow an accepted submission. Keep polling the
        // provisional state; retry only reads, never the submission itself.
        await refreshRelated(client, variables.item);
      }
      setNotice({ text: errorMessage(error), error: true });
    },
  });
  const visible = showProcessed ? items : items.filter((item) => !FINISHED.has(item.status));

  return <>
    <Message notice={notice} />
    <section className={styles.questionList}>
      {visible.length > 0 ? visible.map((item) => <QuestionnaireCard
        item={item}
        key={`${item.account_id}:${item.id}`}
        busy={(action.isPending && action.variables?.item.id === item.id) || item.status === "SUBMITTING"}
        conflict={conflicts[item.id]}
        serverReset={serverReset[item.id] ?? 0}
        onKeepLocal={() => setConflicts((current) => {
          const next = { ...current };
          delete next[item.id];
          return next;
        })}
        onUseServer={() => {
          setConflicts((current) => {
            const next = { ...current };
            delete next[item.id];
            return next;
          });
          setServerReset((current) => ({ ...current, [item.id]: (current[item.id] ?? 0) + 1 }));
        }}
        onSave={(coverLetter, answers) => action.mutate({ kind: "save", item, values: { cover_letter: coverLetter, answers } })}
        onConfirm={(coverLetter, answers) => action.mutate({ kind: "confirm", item, values: { cover_letter: coverLetter, answers } })}
        onSkip={() => action.mutate({ kind: "skip", item })}
      />) : <EmptyState>Нет анкет, ожидающих решения.</EmptyState>}
    </section>
  </>;
}

function QuestionnaireCard({
  item,
  busy,
  onSave,
  onConfirm,
  onSkip,
  conflict,
  serverReset,
  onKeepLocal,
  onUseServer,
}: {
  item: Questionnaire;
  busy: boolean;
  onSave: (letter: string, answers: QuestionnaireAnswer[]) => void;
  onConfirm: (letter: string, answers: QuestionnaireAnswer[]) => void;
  onSkip: () => void;
  conflict?: QuestionnaireConflict;
  serverReset: number;
  onKeepLocal: () => void;
  onUseServer: () => void;
}) {
  const [letter, setLetter] = useState(item.cover_letter);
  const [answers, setAnswers] = useState<Record<string, string | string[]>>(() => Object.fromEntries((item.ai_payload.answers ?? []).map((answer) => [answer.field_id, answer.value])));
  useEffect(() => {
    setLetter(item.cover_letter);
    setAnswers(Object.fromEntries((item.ai_payload.answers ?? []).map((answer) => [answer.field_id, answer.value])));
    // serverReset is an explicit user choice; ordinary query refreshes preserve local edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverReset]);
  const savedAnswers = item.questions
    .map((question) => ({ field_id: question.field_id, value: answers[question.field_id] ?? "", answer_type: question.answer_type }))
    .filter((answer) => Array.isArray(answer.value) ? answer.value.length > 0 : answer.value.trim());
  const incomplete = item.questions.some((question) => {
    const value = answers[question.field_id];
    return question.required && (Array.isArray(value) ? value.length === 0 : !value?.trim());
  });
  const finished = FINISHED.has(item.status);
  const reviewLocked = REVIEW_LOCKED.has(item.status);

  return <article className={`${styles.question} ${finished ? styles.finished : ""}`}>
    <div className={styles.row}>
      <div>
        <div className={styles.questionTitle}>{item.vacancy_title || "Вакансия"}</div>
        {item.vacancy_url ? <a href={item.vacancy_url} target="_blank" rel="noreferrer">Открыть вакансию на hh.ru</a> : null}
        <div className={styles.meta}>Резюме: {item.resume_title || "не определено"}</div>
      </div>
      <span className={`${styles.status} ${finished ? styles.good : ""}`}>{statusText(item.status)}</span>
    </div>
    {item.error_text ? <div className={`${styles.notice} ${styles.error}`} style={{ marginTop: 10 }}>{item.error_text}</div> : null}
    {conflict ? <div className={`${styles.notice} ${styles.warningNotice}`} style={{ marginTop: 10 }}>
      <strong>Конфликт версий</strong>
      <p className={styles.meta}>Серверное письмо: «{conflict.server.cover_letter || "пусто"}»</p>
      <p className={styles.meta}>Ваше письмо: «{conflict.localLetter || "пусто"}»</p>
      <p className={styles.meta}>Серверных ответов: {conflict.server.ai_payload.answers?.length ?? 0}; ваших: {conflict.localAnswers.length}.</p>
      <div className={styles.actionRow}>
        <Button className={styles.secondary} onClick={onKeepLocal}>Продолжить с моими ответами</Button>
        <Button className={styles.secondary} onClick={onUseServer}>Загрузить серверные ответы</Button>
      </div>
    </div> : null}
    {finished ? <p className={styles.meta}>Решение уже принято. Ссылка только показывает текущее состояние и ничего не отправляет повторно.</p> : <>
      {reviewLocked ? <p className={styles.meta}>Проверьте результат на hh.ru и зафиксируйте его во вкладке «История». До этого повторная отправка заблокирована, чтобы не создать дубликат.</p> : null}
      <label className={styles.field} style={{ marginTop: 12 }}>Сопроводительное письмо<textarea disabled={busy} value={letter} onChange={(event) => setLetter(event.target.value)} /></label>
      {item.questions.length > 0 ? <details><summary>Ответы на вопросы ({item.questions.length})</summary><div className={styles.form} style={{ marginTop: 10 }}>
        {item.questions.map((question) => question.answer_type === "unsupported" ? <div className={`${styles.notice} ${styles.warningNotice}`} key={question.field_id}>Поле «{question.label}» имеет неподдерживаемый тип. Заполните его вручную на hh.ru.</div> : question.answer_type === "checkbox" && question.options.length > 0 ? <fieldset className={styles.field} key={question.field_id}><legend>{question.required ? "* " : ""}{question.label}</legend>
          {question.options.map((option) => {
            const selected = Array.isArray(answers[question.field_id]) ? answers[question.field_id] as string[] : [];
            return <label key={option}><input type="checkbox" disabled={busy} checked={selected.includes(option)} onChange={(event) => setAnswers((current) => ({
              ...current,
              [question.field_id]: event.target.checked ? [...selected, option] : selected.filter((value) => value !== option),
            }))} />{option}</label>;
          })}</fieldset> : <label className={styles.field} key={question.field_id}>{question.required ? "* " : ""}{question.label}
          {question.options.length > 0 ? <select disabled={busy} value={String(answers[question.field_id] ?? "")} onChange={(event) => setAnswers((current) => ({ ...current, [question.field_id]: event.target.value }))}>
            <option value="">Выберите ответ</option>{question.options.map((option) => <option value={option} key={option}>{option}</option>)}
          </select> : <input disabled={busy} value={String(answers[question.field_id] ?? "")} onChange={(event) => setAnswers((current) => ({ ...current, [question.field_id]: event.target.value }))} />}
        </label>)}
      </div></details> : null}
      <div className={styles.actionRow} style={{ marginTop: 12 }}>
        <Button className={styles.secondary} disabled={busy} onClick={() => onSave(letter, savedAnswers)}>Сохранить</Button>
        {!reviewLocked ? <Button disabled={busy || incomplete} onClick={() => onConfirm(letter, savedAnswers)}>Подтвердить отправку</Button> : null}
        <Button className={`${styles.secondary} ${styles.danger}`} disabled={busy} onClick={onSkip}>Пропустить</Button>
      </div>
    </>}
  </article>;
}
