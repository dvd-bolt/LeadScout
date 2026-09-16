import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Button, Card } from "../shared/ui";
import { AccountSettings } from "../features/accounts/AccountSettings";
import { LoginPanel } from "../features/login/LoginPanel";
import { ThemePicker } from "../features/theme/ThemePicker";
import { api } from "../shared/http/api";
import type { Dashboard } from "../shared/types/api";
import styles from "../shared/ui/UI.module.css";

export function SettingsPage({ dashboard }: { dashboard: Dashboard }) {
  const client = useQueryClient();
  const active = dashboard.accounts.find((item) => item.id === dashboard.active_account_id);
  const cancel = useMutation({
    mutationFn: () => api.cancelLogin(active?.id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  return <section className={styles.page}>
    <h1 className={styles.pageTitle}>Настройки</h1>
    <ThemePicker />
    {active?.session_status === "AUTH_PENDING" ? <Card>
      <h2>Подключение аккаунта не завершено</h2>
      <p>Завершите ввод кода ниже или отмените подключение. Синхронизация и автоматизация будут доступны после входа.</p>
      <div style={{ marginTop: 12 }}>
        <Button className={styles.secondary} disabled={cancel.isPending} type="button" onClick={() => cancel.mutate()}>
          {cancel.isPending ? "Отменяем…" : "Отменить подключение"}
        </Button>
      </div>
    </Card> : active ? <AccountSettings account={active} key={active.id} /> : null}
    {dashboard.role === "ROOT" || dashboard.role === "ADMIN" ? <Card><h2>Администрирование</h2><p>Доступ, состояние системы и задания</p><Link className={styles.button} to="/admin">Открыть</Link></Card> : null}
    <LoginPanel />
  </section>;
}
