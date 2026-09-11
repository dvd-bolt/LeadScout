import { Link } from "react-router-dom";
import type { Event } from "../types/api";
import { eventStatusLabel, formatDate, formatMoscowTime } from "../lib/format";
import { Icon } from "./Icon";
import styles from "./EventRow.module.css";

export function EventRow({ event, to }: { event: Event; to?: string }) {
  const contents = <><span className={styles.icon}><Icon name={event.status.startsWith("APPLIED") ? "send" : event.status.startsWith("ERROR") ? "alert" : "chat"} size={25} /></span>
    <span className={styles.body}><strong>{event.vacancy_title || "Вакансия"}</strong><small>{eventStatusLabel(event.status)}</small>{event.company ? <small>{event.company}</small> : null}</span>
    <time className={styles.time} title={`${formatDate(event.created_at)} МСК`}>{formatMoscowTime(event.created_at)}</time>{to ? <Icon name="chevron" size={17} /> : null}</>;
  return to ? <Link to={to} className={styles.event}>{contents}</Link> : <div className={styles.event}>{contents}</div>;
}
