import { Link } from "react-router-dom";
import { Icon } from "../../shared/ui/Icon";
import local from "./AutomationControls.module.css";
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { errorMessage } from "../../shared/lib/format";
import type { Account, AutomationStartResult } from "../../shared/types/api";
import { Button, Message, type Notice } from "../../shared/ui";
import styles from "../../shared/ui/UI.module.css";

const STARTED_STATES = new Set(["STARTED", "ALREADY_RUNNING", "QUEUED"]);

function resultLabel(status: string) {
  switch (status) {
    case "STARTED": return "Запущен";
    case "ALREADY_RUNNING": return "Уже выполняется";
    case "QUEUED": return "Поставлен в очередь";
    case "NEEDS_LOGIN": return "Нужен повторный вход";
    case "NEEDS_RESUME": return "Не выбрано готовое резюме";
    case "DAILY_LIMIT_REACHED": return "Дневной лимит исчерпан";
    case "DISABLED": return "Автоматизация отключена";
    default: return `Не запущен: ${status || "неизвестная причина"}`;
  }
}

export function AutomationControls({ active, accounts }: { active: Account; accounts: Account[] }) {
  const client = useQueryClient();
  const [notice, setNotice] = useState<Notice>(null);
  const [results, setResults] = useState<AutomationStartResult[]>([]);
  const enabled = active.auto_apply_enabled || active.automation_state === "SEARCHING";
  const limitReached = active.applied_today >= active.daily_limit;
  const automation = useMutation({
    mutationFn: () => enabled ? api.stopAutomation(active.id) : api.startAutomation(active.id),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: ["dashboard"] });
      setNotice({ tone: "success", text: "Настройка автоматизации обновлена" });
    },
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
  });
  const startAll = useMutation({
    mutationFn: api.startAll,
    onSuccess: async (data) => {
      setResults(data.results);
      await client.invalidateQueries({ queryKey: ["dashboard"] });
      const everyStarted = data.results.length > 0 && data.results.every((item) => STARTED_STATES.has(item.status));
      setNotice({
        text: everyStarted ? "Запуск запрошен для всех аккаунтов" : "Часть аккаунтов не запущена. Проверьте результаты ниже.",
        error: !everyStarted,
      });
    },
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
  });
  const stopAll = useMutation({
    mutationFn: api.stopAll,
    onSuccess: async () => {
      setResults([]);
      await client.invalidateQueries({ queryKey: ["dashboard"] });
      setNotice({ tone: "success", text: "Все автоматизации остановлены" });
    },
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
  });

  const busy = automation.isPending || startAll.isPending || stopAll.isPending;
  return <div className={local.controls}>
    <Message notice={notice} />
    <Button className={local.primary} aria-busy={automation.isPending} disabled={busy || (!enabled && (!active.resume_ready || active.session_status !== "ACTIVE" || limitReached))} onClick={() => automation.mutate()}>
      <Icon name={enabled ? "stop" : "play"} size={26} />{automation.isPending ? "Обновляем…" : enabled ? "Остановить поиск" : "Запустить поиск"}<Icon name="chevron" size={20} />
    </Button>
    <Link to="/settings" className={`${styles.button} ${styles.secondary} ${local.parameters}`}><Icon name="sliders" size={26} />Параметры<Icon name="chevron" size={20} /></Link>
    <details className={local.all}><summary>Все аккаунты</summary>
      <div className={styles.actionRow}>
        <Button className={styles.secondary} disabled={busy || accounts.length === 0} onClick={() => { setResults([]); startAll.mutate(); }}>Запустить все</Button>
        <Button className={`${styles.secondary} ${styles.dangerButton}`} disabled={busy} onClick={() => stopAll.mutate()}>Остановить всё</Button>
      </div>
      {results.length > 0 ? <div className={styles.resultList} aria-label="Результаты массового запуска">
        {results.map((result) => {
          const account = accounts.find((item) => item.id === result.account_id);
          const started = STARTED_STATES.has(result.status);
          return <div className={styles.resultRow} key={result.account_id}>
            <strong>{account?.account_name || `Аккаунт #${result.account_id}`}</strong>
            <span className={started ? styles.good : styles.warning}>{result.message || resultLabel(result.status)}</span>
          </div>;
        })}
      </div> : null}
    </details>
  </div>;
}
