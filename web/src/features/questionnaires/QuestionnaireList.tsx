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

const FINISHED = new Set(["SUBMITTED", "SKIPPED"]);

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
      const updated = await api.updateQuestionnaire(item.id, values ?? {});
      if (kind === "confirm") {
        cacheQuestionnaire(client, { ...updated, status: "SUBMITTING" });
        await api.confirmQuestionnaire(item.id, updated.revision);
        return api.questionnaire(item.id);
      }
      return updated;
    },
    onSuccess: async (updated, variables) => {
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
          setNotice({ text: `Анкета уже изменилась. Актуальное состояние: ${statusText(current.status)}.`, error: true });
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
        key={`${item.account_id}:${item.id}:${item.revision}`}
        busy={(action.isPending && action.variables?.item.id === item.id) || item.status === "SUBMITTING"}
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
}: {
  item: Questionnaire;
  busy: boolean;
  onSave: (letter: string, answers: QuestionnaireAnswer[]) => void;
  onConfirm: (letter: string, answers: QuestionnaireAnswer[]) => void;
  onSkip: () => void;
}) {
  const [letter, setLetter] = useState(item.cover_letter);
  const [answers, setAnswers] = useState<Record<string, string>>(() => Object.fromEntries((item.ai_payload.answers ?? []).map((answer) => [answer.field_id, answer.value])));
  const savedAnswers = item.questions
    .map((question) => ({ field_id: question.field_id, value: answers[question.field_id] ?? "", answer_type: question.answer_type }))
    .filter((answer) => answer.value.trim());
  const incomplete = item.questions.some((question) => question.required && !answers[question.field_id]?.trim());
  const finished = FINISHED.has(item.status);

  return <article className={`${styles.question} ${finished ? styles.finished : ""}`}>
    <div className={styles.row}>
      <div>
        <div className={styles.questionTitle}>{item.vacancy_title || "Вакансия"}</div>
        <div className={styles.meta}>Резюме: {item.resume_title || "не определено"}</div>
      </div>
      <span className={`${styles.status} ${finished ? styles.good : ""}`}>{statusText(item.status)}</span>
    </div>
    {item.error_text ? <div className={`${styles.notice} ${styles.error}`} style={{ marginTop: 10 }}>{item.error_text}</div> : null}
    {finished ? <p className={styles.meta}>Решение уже принято. Ссылка только показывает текущее состояние и ничего не отправляет повторно.</p> : <>
      <label className={styles.field} style={{ marginTop: 12 }}>Сопроводительное письмо<textarea disabled={busy} value={letter} onChange={(event) => setLetter(event.target.value)} /></label>
      {item.questions.length > 0 ? <details><summary>Ответы на вопросы ({item.questions.length})</summary><div className={styles.form} style={{ marginTop: 10 }}>
        {item.questions.map((question) => <label className={styles.field} key={question.field_id}>{question.required ? "* " : ""}{question.label}
          {question.options.length > 0 ? <select disabled={busy} value={answers[question.field_id] ?? ""} onChange={(event) => setAnswers((current) => ({ ...current, [question.field_id]: event.target.value }))}>
            <option value="">Выберите ответ</option>{question.options.map((option) => <option value={option} key={option}>{option}</option>)}
          </select> : <input disabled={busy} value={answers[question.field_id] ?? ""} onChange={(event) => setAnswers((current) => ({ ...current, [question.field_id]: event.target.value }))} />}
        </label>)}
      </div></details> : null}
      <div className={styles.actionRow} style={{ marginTop: 12 }}>
        <Button className={styles.secondary} disabled={busy} onClick={() => onSave(letter, savedAnswers)}>Сохранить</Button>
        <Button disabled={busy || incomplete} onClick={() => onConfirm(letter, savedAnswers)}>Подтвердить отправку</Button>
        <Button className={`${styles.secondary} ${styles.danger}`} disabled={busy} onClick={onSkip}>Пропустить</Button>
      </div>
    </>}
  </article>;
}
