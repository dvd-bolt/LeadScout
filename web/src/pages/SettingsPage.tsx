import { AccountSettings } from "../features/accounts/AccountSettings";
import { LoginPanel } from "../features/login/LoginPanel";
import type { Dashboard } from "../shared/types/api";
import styles from "../shared/ui/UI.module.css";

export function SettingsPage({ dashboard }: { dashboard: Dashboard }) {
  const active = dashboard.accounts.find((item) => item.id === dashboard.active_account_id);
  return <section className={styles.page}>
    <h1 className={styles.pageTitle}>Настройки</h1>
    {active ? <AccountSettings account={active} key={active.id} /> : null}
    <LoginPanel />
  </section>;
}
