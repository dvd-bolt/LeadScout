import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Icon } from "./Icon";
import styles from "./UI.module.css";

export type Notice = { text: string; error?: boolean; tone?: "info" | "success" | "warning" } | null;

export function Button({ children, className = "", ...props }: ButtonHTMLAttributes<HTMLButtonElement>) {
  return <button className={`${styles.button} ${className}`} {...props}>{children}</button>;
}
export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`${styles.card} ${className}`}>{children}</section>;
}
export function Field({ children, label }: { children: ReactNode; label: string }) {
  return <label className={styles.field}>{label}{children}</label>;
}
export function Badge({ count }: { count: number }) {
  return count > 0 ? <span className={styles.badge} aria-label={`${count} анкет требуют проверки`}>{count > 99 ? "99+" : count}</span> : null;
}
export function Message({ notice }: { notice: Notice }) {
  if (!notice) return null;
  return <div role="status" className={`${styles.notice} ${notice.error ? styles.error : notice.tone === "warning" ? styles.warningNotice : notice.tone === "success" ? styles.successNotice : ""}`}><Icon name={notice.error || notice.tone === "warning" ? "alert" : notice.tone === "success" ? "check" : "info"} size={20} /><span>{notice.text}</span></div>;
}
export function EmptyState({ children }: { children: ReactNode }) {
  return <Card className={styles.empty}>{children}</Card>;
}
