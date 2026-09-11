import { useEffect, useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, NavLink, useLocation } from "react-router-dom";
import { api } from "../shared/http/api";
import { telegram } from "../shared/telegram/sdk";
import type { Dashboard } from "../shared/types/api";
import { Badge, Message, type Notice } from "../shared/ui";
import { errorMessage } from "../shared/lib/format";
import { Icon, type IconName } from "../shared/ui/Icon";
import styles from "./AppLayout.module.css";

function NavItem({ to, label, icon, count = 0, end = false }: { to: string; label: string; icon: IconName; count?: number; end?: boolean }) {
  return <NavLink to={to} end={end} aria-label={label} aria-description={count > 0 ? `${count} анкет требуют проверки` : undefined} className={({ isActive }) => `${styles.navLink} ${isActive ? styles.navLinkActive : ""}`}>
    <span className={styles.navIcon}><Icon name={icon} size={28} /><Badge count={count} /></span><span>{label}</span>
  </NavLink>;
}

export function AppLayout({ dashboard, children }: { dashboard: Dashboard; children: ReactNode }) {
  const client = useQueryClient();
  const location = useLocation();
  const [notice, setNotice] = useState<Notice>(null);
  const active = dashboard.accounts.find((item) => item.id === dashboard.active_account_id);
  const activate = useMutation({
    mutationFn: api.activateAccount,
    onSuccess: () => client.invalidateQueries({ queryKey: ["dashboard"] }),
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
  });

  useEffect(() => {
    const app = telegram();
    if (!app) return;
    const goBack = () => history.back();
    if (location.pathname !== "/") app.BackButton.show(); else app.BackButton.hide();
    app.BackButton.onClick(goBack);
    return () => app.BackButton.offClick(goBack);
  }, [location.pathname]);

  return <main className={styles.shell}>
    <header className={styles.header}>
      <Link to="/" className={styles.brand} aria-label="LeadScout — главная"><Icon name="arrow" size={29} /><span>LeadScout</span></Link>
      {dashboard.accounts.length > 0 ? <div className={styles.account}><Icon name="user" size={19} /><select
        aria-label="Активный аккаунт"
        className={styles.accountSelect}
        title={active?.account_name}
        disabled={activate.isPending}
        value={active?.id ?? ""}
        onChange={(event) => { setNotice(null); activate.mutate(Number(event.target.value)); }}
      >
        {dashboard.accounts.map((account) => <option value={account.id} key={account.id}>{account.account_name}</option>)}
      </select></div> : null}
    </header>
    <Message notice={notice} />
    {children}
    <nav aria-label="Основная навигация" className={styles.bottomNav}>
      <NavItem to="/" label="Главная" icon="home" end />
      <NavItem to="/applications" label="Отклики" icon="inbox" count={dashboard.active_pending_review_count} />
      <NavItem to="/resumes" label="Резюме" icon="resume" />
      <NavItem to="/settings" label="Настройки" icon="settings" />
    </nav>
  </main>;
}
