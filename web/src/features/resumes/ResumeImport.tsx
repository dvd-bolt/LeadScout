import { useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { errorMessage } from "../../shared/lib/format";
import type { NeedsFieldsResult, Operation, StructuredResume } from "../../shared/types/api";
import { Button, Message, type Notice } from "../../shared/ui";
import { OperationStatus, operationNeedsInput, operationSucceeded } from "../../shared/ui/OperationStatus";
import styles from "../../shared/ui/UI.module.css";

const FIELD_LABELS: Record<string, string> = {
  first_name: "Имя",
  birth_date: "Дата рождения",
  city: "Город",
  title: "Желаемая должность",
};

function needsFields(operation: Operation): NeedsFieldsResult | null {
  if (!operationNeedsInput(operation)) return null;
  const result = operation.result;
  if (!Array.isArray(result.missing_fields) || typeof result.structured !== "object" || !result.structured) return null;
  return result as NeedsFieldsResult;
}

export function ResumeImport({ accountId }: { accountId: number }) {
  const client = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [operationId, setOperationId] = useState<string | null>(null);
  const [operationActive, setOperationActive] = useState(false);
  const [requiredData, setRequiredData] = useState<NeedsFieldsResult | null>(null);
  const [notice, setNotice] = useState<Notice>(null);
  const upload = useMutation({
    mutationFn: (structured?: StructuredResume) => api.importResume(accountId, file!, structured),
    onSuccess: (data) => {
      setRequiredData(null);
      setNotice(null);
      setOperationId(data.operation_id);
      setOperationActive(true);
    },
    onError: (error) => {
      setOperationActive(false);
      setNotice({ text: errorMessage(error), error: true });
    },
  });
  const busy = upload.isPending || operationActive;

  const handleTerminal = (operation: Operation) => {
    setOperationActive(false);
    const needs = needsFields(operation);
    if (needs) {
      setRequiredData(needs);
      return;
    }
    if (operationSucceeded(operation)) {
      setFile(null);
      if (inputRef.current) inputRef.current.value = "";
      client.invalidateQueries({ queryKey: ["resumes", accountId] });
      client.invalidateQueries({ queryKey: ["dashboard"] });
    }
  };

  return <div className={styles.form}>
    <form className={styles.form} onSubmit={(event) => { event.preventDefault(); setNotice(null); upload.mutate(undefined); }}>
      <label className={styles.field}>PDF для импорта в hh.ru<input
        ref={inputRef}
        accept="application/pdf"
        disabled={busy}
        type="file"
        onChange={(event) => { setFile(event.target.files?.[0] ?? null); setRequiredData(null); setOperationId(null); setNotice(null); }}
      /></label>
      <Button disabled={!file || busy} type="submit">Импортировать в hh.ru</Button>
    </form>
    <OperationStatus operationId={operationId} scopeKey={accountId} onTerminal={handleTerminal} onError={() => setOperationActive(false)} />
    <Message notice={notice} />
    {requiredData && file ? <form className={`${styles.form} ${styles.followupForm}`} onSubmit={(event) => {
      event.preventDefault();
      const values = new FormData(event.currentTarget);
      const structured: StructuredResume = { ...requiredData.structured };
      requiredData.missing_fields.forEach((field) => { structured[field] = String(values.get(field) || "").trim(); });
      setNotice(null);
      upload.mutate(structured);
    }}>
      <h3>Дополните данные</h3>
      <p className={styles.meta}>{requiredData.message || "Эти поля нужны мастеру создания резюме hh.ru. Остальная распознанная структура сохранена."}</p>
      {requiredData.missing_fields.map((field) => <label className={styles.field} key={field}>{FIELD_LABELS[field] || field}
        <input required name={field} type={field === "birth_date" ? "date" : "text"} defaultValue={typeof requiredData.structured[field] === "string" ? requiredData.structured[field] as string : ""} />
      </label>)}
      <Button disabled={busy} type="submit">Подтвердить и продолжить импорт</Button>
    </form> : null}
  </div>;
}
