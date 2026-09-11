import { Link } from "react-router-dom";
import { AutomationControls } from "../features/automation/AutomationControls";
import { accountStatusLabel, formatMoscowTime, reviewCountLabel } from "../shared/lib/format";
import type { Dashboard } from "../shared/types/api";
import { EmptyState } from "../shared/ui";
import { EventRow } from "../shared/ui/EventRow";
import { SegmentedProgress } from "../shared/ui/SegmentedProgress";
import { Icon } from "../shared/ui/Icon";
import ui from "../shared/ui/UI.module.css";
import styles from "./HomePage.module.css";

export function HomePage({ dashboard }: { dashboard: Dashboard }) {
  const active = dashboard.accounts.find((account) => account.id === dashboard.active_account_id);
  if (!active) return <section className={styles.page}><h1 className={styles.title}>Начнём<br />поиск</h1>
    <p>Подключите hh.ru, выберите резюме и настройте первые отклики.</p>
    <section className={styles.onboarding}><h2>Первый запуск</h2><ol><li>Подключите аккаунт hh.ru</li><li>Синхронизируйте и выберите резюме</li><li>Задайте параметры поиска и лимит</li></ol><Link className={ui.button} to="/settings">Открыть настройки<Icon name="arrow" size={20} /></Link></section>
  </section>;
  const pending = dashboard.active_pending_review_count ?? 0;
  const validLimit = Number.isFinite(active.daily_limit) && active.daily_limit > 0;
  const validCount = Number.isFinite(active.applied_today) && active.applied_today >= 0;
  const percent = validLimit && validCount ? Math.round(Math.min(100, Math.max(0, active.applied_today / active.daily_limit * 100))) : null;
  const scheduled = active.auto_apply_enabled && dashboard.next_scheduled_search_at;
  const status = accountStatusLabel(active);
  return <section className={styles.page}>
    <h1 className={styles.title}>Поиск под<br />контролем</h1>
    <div className={styles.metrics}>
      <section className={`${styles.metric} ${styles.progressMetric}`} aria-label="Дневной лимит откликов">
        <div className={styles.numbers}>{validCount ? active.applied_today : "—"}<span>/ {validLimit ? active.daily_limit : "—"}</span></div>
        <span className={styles.metricLabel}>Отклики сегодня</span><div className={styles.percent}>{percent === null ? "—" : `${percent}%`}</div>
        <SegmentedProgress percent={percent} />
      </section>
      <Link to="/applications?tab=review" className={`${styles.metric} ${styles.metricButton}`} aria-label={`${pending} анкет ждут проверки`}><div className={styles.smallTop}><strong>{pending}</strong><Icon name="users" /></div><span className={styles.metricLabel}>Ждут проверки</span></Link>
      <section className={styles.metric}><div className={styles.smallTop}>{scheduled ? <strong>{formatMoscowTime(scheduled)}</strong> : <span className={styles.timeText}>Не запланирован</span>}{scheduled ? <Icon name="clock" size={18} /> : null}</div><span className={styles.caption}>Следующий запуск{scheduled ? " · МСК" : ""}</span></section>
    </div>
    <section className={styles.selection}>
      <Link to="/resumes" className={styles.resumeLink}><Icon name="resume" size={27} /><span><span className={styles.caption}>Выбранное резюме</span><strong>{active.active_resume_title || "Выберите резюме"}</strong></span></Link>
      <div className={styles.status}><span className={styles.caption}>Автоматизация</span><span className={styles.statusValue}>{status.tone === "warning" || status.tone === "danger" ? <Icon name="alert" size={17} /> : <span className={styles.dot} />}{status.text}</span></div>
    </section>
    <AutomationControls key={active.id} active={active} accounts={dashboard.accounts} />
    {pending > 0 ? <Link className={styles.review} to="/applications?tab=review"><Icon name="inbox" size={27} /><span>{reviewCountLabel(pending)}</span><Icon name="chevron" size={20} /></Link> : null}
    <section className={styles.events}><h2>Последние события</h2>{dashboard.recent_events.length ? dashboard.recent_events.map(event => <EventRow key={event.id} event={event} to="/applications?tab=history" />) : <EmptyState>Событий ещё нет. После запуска здесь появится история поиска.</EmptyState>}</section>
  </section>;
}
