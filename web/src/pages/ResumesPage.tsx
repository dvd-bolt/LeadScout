import { useSearchParams } from "react-router-dom";
import { AuditPanel } from "../features/audits/AuditPanel";
import { ResumeManager } from "../features/resumes/ResumeManager";
import type { Dashboard } from "../shared/types/api";
import styles from "../shared/ui/UI.module.css";

export function ResumesPage({ dashboard }: { dashboard: Dashboard }) {
  const [searchParams] = useSearchParams();
  const highlightedSnapshotId = Number(searchParams.get("snapshot_id")) || undefined;
  const focusAudit = searchParams.get("focus") === "audit";
  const active = dashboard.accounts.find((item) => item.id === dashboard.active_account_id);
  const resumes = active?.session_status === "AUTH_PENDING" ? <section className={`${styles.card} ${styles.empty}`}>Подключение аккаунта не завершено. Сначала введите код или отмените вход в настройках.</section>
    : dashboard.active_account_id ? <ResumeManager dashboard={dashboard} highlightedSnapshotId={highlightedSnapshotId} />
    : <section className={`${styles.card} ${styles.empty}`}>Для синхронизации и импорта резюме подключите аккаунт hh.ru. Независимый аудит доступен ниже.</section>;
  const audits = <AuditPanel key={`audit:${dashboard.active_account_id ?? "none"}`} accountId={dashboard.active_account_id ?? undefined} snapshotId={highlightedSnapshotId} focus={focusAudit} />;

  return <section className={styles.page}>
    <h1 className={styles.pageTitle}>Резюме</h1>
    {focusAudit ? <>{audits}{resumes}</> : <>{resumes}{audits}</>}
  </section>;
}
