import styles from './SegmentedProgress.module.css';

export function SegmentedProgress({ percent }: { percent: number | null }) {
  const value = percent === null ? null : Math.min(100, Math.max(0, percent));
  return <div className={styles.track} role="progressbar" aria-label="Использовано дневного лимита" aria-valuemin={0} aria-valuemax={100} aria-valuenow={value ?? undefined} aria-valuetext={value === null ? 'Лимит не задан' : `${value}%`}>
    {Array.from({ length: 20 }, (_, index) => <span className={styles.segment} key={index} aria-hidden="true"><span style={{ width: `${Math.min(100, Math.max(0, (value ?? 0) * 20 - index * 100))}%` }} /></span>)}
  </div>;
}
