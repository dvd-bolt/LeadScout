import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { errorMessage, formatDate } from "../../shared/lib/format";
import type { Operation, ResumeDraft } from "../../shared/types/api";
import { Button, Message, type Notice } from "../../shared/ui";
import { OperationStatus } from "../../shared/ui/OperationStatus";
import { ResumeWizard } from "./ResumeWizard";
import styles from "../../shared/ui/UI.module.css";

export function ResumeImport({ accountId }: { accountId: number }) {
  const client = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const [pendingDraftId, setPendingDraftId] = useState<number | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const drafts = useQuery({
    queryKey: ["resume-drafts", accountId],
    queryFn: () => api.resumeDrafts(accountId),
  });
  const selected = drafts.data?.find((item) => item.id === selectedId) ?? null;

  useEffect(() => {
    if (selectedId !== null || pendingDraftId !== null || !drafts.data?.length) return;
    const resumable = drafts.data.find((item) => item.status !== "COMPLETED") ?? drafts.data[0];
    setSelectedId(resumable.id);
  }, [drafts.data, pendingDraftId, selectedId]);

  const create = useMutation({
    mutationFn: (source: "MANUAL" | "PDF") => api.createResumeDraft(accountId, source),
    onSuccess: async (draft) => {
      await client.invalidateQueries({ queryKey: ["resume-drafts", accountId] });
      setSelectedId(draft.id);
      setNotice(null);
    },
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteResumeDraft(accountId, id),
    onSuccess: async () => {
      setSelectedId(null);
      await client.invalidateQueries({ queryKey: ["resume-drafts", accountId] });
    },
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
  });
  const refresh = async () => client.invalidateQueries({ queryKey: ["resume-drafts", accountId] });
  const rememberDraft = useCallback((updated: ResumeDraft) => {
    client.setQueryData<ResumeDraft[]>(["resume-drafts", accountId], (current) =>
      current?.map((item) => item.id === updated.id ? updated : item) ?? [updated]);
  }, [accountId, client]);

  const startPdf = async (file: File) => {
    try {
      const draft = await api.createResumeDraft(accountId, "PDF");
      setPendingDraftId(draft.id);
      await refresh();
      const result = await api.extractResumePdf(accountId, draft.id, file);
      setOperationId(result.operation_id);
      setNotice({ text: "PDF принят. Распознаю данные в локальный черновик.", tone: "info" });
    } catch (error) {
      setNotice({ text: errorMessage(error), error: true });
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const terminal = async (operation: Operation) => {
    await refresh();
    if (pendingDraftId !== null) setSelectedId(pendingDraftId);
    setPendingDraftId(null);
    const message = typeof operation.result.message === "string" ? operation.result.message : operation.error_text;
    setNotice(message ? { text: message, error: operation.status === "FAILED", tone: operation.status === "SUCCEEDED" ? "success" : "warning" } : null);
  };

  if (selected) {
    return <ResumeWizard
      accountId={accountId}
      draft={selected}
      onRefresh={refresh}
      onSaved={rememberDraft}
      onClose={() => setSelectedId(null)}
      onDelete={() => {
        if (window.confirm("Удалить локальный черновик? Резюме на hh.ru удалено не будет.")) remove.mutate(selected.id);
      }}
    />;
  }

  return <div className={styles.form}>
    <Message notice={drafts.isError ? { text: errorMessage(drafts.error), error: true } : notice} />
    <OperationStatus operationId={operationId} scopeKey={`${accountId}:pdf`} onTerminal={(item) => void terminal(item)} />
    <div className={styles.startChoices}>
      <button className={styles.startChoice} disabled={create.isPending} onClick={() => create.mutate("MANUAL")}>
        <strong>Создать с нуля</strong><span>Пустой сохраняемый черновик со всеми разделами.</span>
      </button>
      <label className={styles.startChoice}>
        <strong>Заполнить из PDF</strong><span>PDF только распознаётся в LeadScout и не отправляется в файловые поля hh.ru.</span>
        <input ref={fileRef} type="file" accept="application/pdf" onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void startPdf(file);
        }} />
      </label>
    </div>
    {drafts.data?.length ? <div className={styles.draftList}>
      <h3>Сохранённые черновики</h3>
      {drafts.data.map((draft: ResumeDraft) => <article className={styles.resume} key={draft.id}>
        <div className={styles.row}>
          <div><div className={styles.resumeTitle}>{draft.data.profession.title || `Черновик №${draft.id}`}</div>
            <div className={styles.meta}>{draft.status} · шаг «{draft.current_step}» · {formatDate(draft.updated_at)}</div>
          </div>
          <Button className={styles.secondary} onClick={() => setSelectedId(draft.id)}>{draft.status === "COMPLETED" ? "Посмотреть" : "Продолжить"}</Button>
        </div>
      </article>)}
    </div> : <div className={styles.empty}>Черновиков пока нет. Начните вручную или из PDF.</div>}
  </div>;
}
