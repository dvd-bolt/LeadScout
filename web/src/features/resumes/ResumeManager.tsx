import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { errorMessage, formatDate } from "../../shared/lib/format";
import type { Dashboard, Operation, Resume } from "../../shared/types/api";
import { Button, Message, type Notice } from "../../shared/ui";
import { OperationStatus, operationSucceeded } from "../../shared/ui/OperationStatus";
import { ResumeImport } from "./ResumeImport";
import styles from "../../shared/ui/UI.module.css";

export function ResumeManager({ dashboard, highlightedSnapshotId }: { dashboard: Dashboard; highlightedSnapshotId?: number }) {
  const accountId = dashboard.active_account_id!;
  const client = useQueryClient();
  const [operationId, setOperationId] = useState<string | null>(null);
  const terminalOperation = useRef<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const resumes = useQuery({ queryKey: ["resumes", accountId], queryFn: () => api.resumes(accountId) });
  const activeSync = useQuery({
    queryKey: ["operations", "resume-sync", accountId],
    queryFn: () => api.operations({ kind: "resume-sync", accountId, resource: String(accountId) }),
    refetchInterval: (query) => query.state.data?.length ? 3000 : false,
  });
  useEffect(() => {
    const current = activeSync.data?.[0];
    if (current && !operationId && current.id !== terminalOperation.current) setOperationId(current.id);
  }, [activeSync.data, operationId]);
  const handleError = (error: unknown) => setNotice({ text: errorMessage(error), error: true });
  const select = useMutation({
    mutationFn: (id: number) => api.activateResume(accountId, id),
    onSuccess: () => client.invalidateQueries({ queryKey: ["dashboard"] }),
    onError: handleError,
  });
  const sync = useMutation({ mutationFn: () => api.syncResumes(accountId), onSuccess: (data) => { terminalOperation.current = null; setNotice(null); setOperationId(data.operation_id); }, onError: handleError });
  const remove = useMutation({ mutationFn: (id: number) => api.deleteResume(accountId, id), onSuccess: (data) => { terminalOperation.current = null; setNotice(null); setOperationId(data.operation_id); }, onError: handleError });
  const handleTerminal = (operation: Operation) => {
    terminalOperation.current = operation.id;
    const message = typeof operation.result.message === "string"
      ? operation.result.message
      : operation.error_text;
    if (message) {
      setNotice({ text: message, error: !operationSucceeded(operation) });
    }
    setOperationId(null);
    void activeSync.refetch();
    if (operationSucceeded(operation)) {
      client.invalidateQueries({ queryKey: ["resumes", accountId] });
      client.invalidateQueries({ queryKey: ["dashboard"] });
    }
  };

  return <section className={styles.card}>
    <div className={styles.cardHeader}><h2>Резюме hh.ru</h2><Button className={styles.secondary} disabled={sync.isPending || Boolean(activeSync.data?.length)} onClick={() => sync.mutate()}>Синхронизировать</Button></div>
    <Message notice={resumes.isError ? { text: resumes.error.message, error: true } : notice} />
    <OperationStatus operationId={operationId} scopeKey={accountId} onTerminal={handleTerminal} />
    <ResumeImport accountId={accountId} />
    <div className={styles.resumeList} style={{ marginTop: 14 }}>
      {resumes.data?.length ? resumes.data.map((resume) => <ResumeRow
        resume={resume}
        active={dashboard.accounts.find((item) => item.id === accountId)?.active_resume_hh_id === resume.hh_resume_id}
        highlighted={highlightedSnapshotId === resume.snapshot_id}
        onSelect={() => select.mutate(resume.snapshot_id)}
        onDelete={() => {
          if (window.confirm(`Удалить «${resume.title}» на hh.ru? Это действие нельзя отменить.`)) remove.mutate(resume.snapshot_id);
        }}
        key={resume.snapshot_id}
      />) : <div className={styles.empty}>Нажми «Синхронизировать», чтобы загрузить список.</div>}
    </div>
  </section>;
}

function ResumeRow({ resume, active, highlighted, onSelect, onDelete }: { resume: Resume; active: boolean; highlighted: boolean; onSelect: () => void; onDelete: () => void }) {
  return <article className={`${styles.resume} ${highlighted ? styles.highlighted : ""}`}>
    <div className={styles.row}>
      <div><div className={styles.resumeTitle}>{resume.title}</div><div className={styles.meta}>{resume.status} · синхр. {formatDate(resume.synced_at)}</div></div>
      <div className={styles.actionRow}>
        {active ? <span className={`${styles.status} ${styles.good}`}>● Выбрано</span> : <Button className={styles.secondary} onClick={onSelect}>Выбрать</Button>}
        <button aria-label={`Удалить ${resume.title}`} className={`${styles.textButton} ${styles.danger}`} onClick={onDelete}>Удалить</button>
      </div>
    </div>
    {resume.extracted_text ? <details><summary>Посмотреть текст</summary><p className={`${styles.meta} ${styles.resumeText}`}>{resume.extracted_text.slice(0, 800)}{resume.extracted_text.length > 800 ? "…" : ""}</p></details> : null}
  </article>;
}
