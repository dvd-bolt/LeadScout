import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { errorMessage, formatDate, resumeStatusLabel, resumeStepLabel } from "../../shared/lib/format";
import type { Operation, ResumeDraft } from "../../shared/types/api";
import { Button, Message, type Notice } from "../../shared/ui";
import { OperationStatus } from "../../shared/ui/OperationStatus";
import { ResumeWizard } from "./ResumeWizard";
import styles from "../../shared/ui/UI.module.css";

export function ResumeImport({ accountId }: { accountId: number }) {
  const client = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const autoOpenedAccount = useRef<number | null>(null);
  const terminalOperation = useRef<string | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const operationKey = `leadscout:operation:pdf:${accountId}`;
  const pendingKey = `leadscout:operation:pdf-draft:${accountId}`;
  const [operationId, setOperationId] = useState<string | null>(() => sessionStorage.getItem(operationKey));
  const [pendingDraftId, setPendingDraftId] = useState<number | null>(() => {
    const value = Number(sessionStorage.getItem(pendingKey));
    return value > 0 ? value : null;
  });
  const [notice, setNotice] = useState<Notice>(null);
  const activeExtract = useQuery({
    queryKey: ["operations", "resume-draft-extract", accountId],
    queryFn: () => api.operations({ kind: "resume-draft-extract", accountId }),
    refetchInterval: (query) => query.state.data?.length ? 2500 : false,
  });
  const drafts = useQuery({
    queryKey: ["resume-drafts", accountId],
    queryFn: () => api.resumeDrafts(accountId),
    refetchInterval: (query) => query.state.data?.some((item) => item.status === "PARSING") ? 2500 : false,
  });
  const selected = drafts.data?.find((item) => item.id === selectedId) ?? null;

  useEffect(() => {
    const active = activeExtract.data?.[0];
    if (!active || operationId || active.id === terminalOperation.current) return;
    const draftId = Number(active.resource);
    setOperationId(active.id);
    if (draftId > 0) setPendingDraftId(draftId);
  }, [activeExtract.data, operationId]);

  useEffect(() => {
    autoOpenedAccount.current = null;
    setSelectedId(null);
    const restoredOperation = sessionStorage.getItem(`leadscout:operation:pdf:${accountId}`);
    const restoredDraft = Number(sessionStorage.getItem(`leadscout:operation:pdf-draft:${accountId}`));
    setPendingDraftId(restoredDraft > 0 ? restoredDraft : null);
    setOperationId(restoredOperation);
    setNotice(null);
  }, [accountId]);

  useEffect(() => {
    if (operationId) sessionStorage.setItem(operationKey, operationId);
    else sessionStorage.removeItem(operationKey);
    if (pendingDraftId) sessionStorage.setItem(pendingKey, String(pendingDraftId));
    else sessionStorage.removeItem(pendingKey);
  }, [operationId, operationKey, pendingDraftId, pendingKey]);

  useEffect(() => {
    if (autoOpenedAccount.current === accountId || selectedId !== null || pendingDraftId !== null || !drafts.data?.length) return;
    const resumable = drafts.data.find((item) => item.status !== "COMPLETED") ?? drafts.data[0];
    autoOpenedAccount.current = accountId;
    setSelectedId(resumable.id);
  }, [accountId, drafts.data, pendingDraftId, selectedId]);

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

  const startPdf = async (file: File, existingDraftId?: number) => {
    try {
      autoOpenedAccount.current = accountId;
      const draftId = existingDraftId ?? (await api.createResumeDraft(accountId, "PDF")).id;
      setSelectedId(null);
      setPendingDraftId(draftId);
      await refresh();
      const result = await api.extractResumePdf(accountId, draftId, file);
      terminalOperation.current = null;
      setOperationId(result.operation_id);
      setNotice({ text: "PDF принят. Распознаю данные в локальный черновик.", tone: "info" });
    } catch (error) {
      setPendingDraftId(null);
      setNotice({ text: errorMessage(error), error: true });
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const terminal = async (operation: Operation) => {
    terminalOperation.current = operation.id;
    await refresh();
    const parsed = operation.status === "SUCCEEDED" && "code" in operation.result && operation.result.code === "PDF_PARSED";
    if (parsed && pendingDraftId !== null) setSelectedId(pendingDraftId);
    setPendingDraftId(null);
    setOperationId(null);
    void activeExtract.refetch();
    const message = typeof operation.result.message === "string" ? operation.result.message : operation.error_text;
    setNotice(message ? { text: message, error: !parsed, tone: parsed ? "success" : "warning" } : null);
  };

  if (selected) {
    return <ResumeWizard
      accountId={accountId}
      draft={selected}
      onRefresh={refresh}
      onSaved={rememberDraft}
      onClose={() => setSelectedId(null)}
      onRetryPdf={selected.source === "PDF" ? (file) => startPdf(file, selected.id) : undefined}
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
      {drafts.data.map((draft: ResumeDraft) => {
        const parseError = draft.validation.parse_error;
        const needsPdfUpload = Boolean(parseError) || (draft.source === "PDF" && draft.status === "DRAFT" && draft.revision === 1);
        const parsing = pendingDraftId === draft.id || draft.status === "PARSING";
        return <article className={styles.resume} key={draft.id}>
        <div className={styles.row}>
          <div><div className={styles.resumeTitle}>{draft.data.profession.title || `Черновик №${draft.id}`}</div>
            <div className={styles.meta}>{resumeStatusLabel(draft.status)} · шаг «{resumeStepLabel(draft.current_step)}» · {formatDate(draft.updated_at)}</div>
          </div>
          {!needsPdfUpload ? <Button className={styles.secondary} disabled={parsing} onClick={() => setSelectedId(draft.id)}>{draft.status === "COMPLETED" ? "Посмотреть" : parsing ? "Распознавание…" : "Продолжить"}</Button> : null}
        </div>
        {needsPdfUpload ? <>
          <div className={`${styles.notice} ${styles.error}`} role="alert">{parseError?.message || "PDF ещё не распознан. Повторите загрузку или заполните черновик вручную."}</div>
          <div className={styles.actionRow}>
            <label aria-disabled={parsing} className={`${styles.button} ${styles.secondary} ${styles.fileAction}`}>
              {parsing ? "Распознавание…" : "Повторить загрузку PDF"}
              <input disabled={parsing} aria-label={`Повторить загрузку PDF для черновика ${draft.id}`} type="file" accept="application/pdf" onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) void startPdf(file, draft.id);
                event.target.value = "";
              }} />
            </label>
            <Button className={styles.secondary} disabled={parsing} onClick={() => setSelectedId(draft.id)}>Заполнить вручную</Button>
            <Button className={styles.dangerButton} disabled={parsing || remove.isPending} onClick={() => {
              if (window.confirm("Удалить пустой локальный черновик?")) remove.mutate(draft.id);
            }}>Удалить черновик</Button>
          </div>
        </> : null}
      </article>})}
    </div> : <div className={styles.empty}>Черновиков пока нет. Начните вручную или из PDF.</div>}
  </div>;
}
