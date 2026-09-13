import { Link } from "react-router-dom";
import type { Event } from "../types/api";
import { applicationStageLabel, eventStatusLabel, formatDate, formatMoscowTime } from "../lib/format";
import { Icon } from "./Icon";
import styles from "./EventRow.module.css";

const ALWAYS_REVIEWABLE = new Set(["ERROR_SUBMIT_UNCONFIRMED", "ERROR_LOCAL_PERSISTENCE"]);
const POST_SUBMIT_REVIEWABLE = new Set(["ERROR_TIMEOUT", "ERROR_BROWSER", "SKIPPED_STOPPED"]);

export function EventRow({
  event,
  to,
  onResolve,
  resolving = false,
}: {
  event: Event;
  to?: string;
  onResolve?: (applied: boolean) => void;
  resolving?: boolean;
}) {
  const reviewable = Boolean(event.attempt_id) && (
    ALWAYS_REVIEWABLE.has(event.status)
    || (POST_SUBMIT_REVIEWABLE.has(event.status) && ["SUBMITTING", "CONFIRMING"].includes(event.stage ?? ""))
  );
  const contents = <><span className={styles.icon}><Icon name={event.status.startsWith("APPLIED") ? "send" : event.status.startsWith("ERROR") ? "alert" : "chat"} size={25} /></span>
    <span className={styles.body}><strong>{event.vacancy_title || "Вакансия"}</strong><small>{eventStatusLabel(event.status)}</small>{event.stage ? <small>Этап: {applicationStageLabel(event.stage)}</small> : null}{event.details ? <small>{event.details}</small> : null}{event.company ? <small>{event.company}</small> : null}
      {reviewable && onResolve ? <span className={styles.reviewActions}>
        <button disabled={resolving} onClick={() => onResolve(true)}>Отклик есть на hh.ru</button>
        <button disabled={resolving} onClick={() => onResolve(false)}>Отклика нет</button>
      </span> : null}
    </span>
    <time className={styles.time} title={`${formatDate(event.created_at)} МСК`}>{formatMoscowTime(event.created_at)}</time>{to ? <Icon name="chevron" size={17} /> : null}</>;
  return to ? <Link to={to} className={styles.event}>{contents}</Link> : <div className={styles.event}>{contents}</div>;
}
