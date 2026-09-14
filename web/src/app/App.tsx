import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { HashRouter, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { AdminPage } from "../pages/AdminPage";
import { ApplicationsPage } from "../pages/ApplicationsPage";
import { HomePage } from "../pages/HomePage";
import { ResumesPage } from "../pages/ResumesPage";
import { SettingsPage } from "../pages/SettingsPage";
import { api } from "../shared/http/api";
import { ApiError, authenticate, setCsrfToken } from "../shared/http/client";
import { errorMessage } from "../shared/lib/format";
import type { Dashboard, Resume } from "../shared/types/api";
import { Button, Message, type Notice } from "../shared/ui";
import { AppLayout } from "./AppLayout";
import styles from "../shared/ui/UI.module.css";

type LaunchTarget = "questionnaire" | "applications" | "resume" | "audit" | "accounts" | "settings";
type LaunchParams = { target?: string; accountId?: number; applyId?: number; snapshotId?: number; invalid?: string };

function parseId(params: URLSearchParams, name: string) {
  const raw = params.get(name);
  if (raw === null) return undefined;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

function readLaunchParams(hashSearch: string): LaunchParams {
  const params = new URLSearchParams(hashSearch);
  new URLSearchParams(window.location.search).forEach((value, key) => params.set(key, value));
  const accountId = parseId(params, "account_id");
  const applyId = parseId(params, "apply_id");
  const snapshotId = parseId(params, "snapshot_id");
  const invalidNames = [["account_id", accountId], ["apply_id", applyId], ["snapshot_id", snapshotId]].filter(([, value]) => value === null).map(([name]) => name);
  return {
    target: params.get("target") ?? undefined,
    accountId: accountId ?? undefined,
    applyId: applyId ?? undefined,
    snapshotId: snapshotId ?? undefined,
    invalid: invalidNames.length ? `Некорректные параметры ссылки: ${invalidNames.join(", ")}.` : undefined,
  };
}

function DirectLinkController({ dashboard }: { dashboard: Dashboard }) {
  const location = useLocation();
  const navigate = useNavigate();
  const client = useQueryClient();
  const initialDashboard = useRef(dashboard).current;
  const [launch] = useState(() => readLaunchParams(location.search));
  const [notice, setNotice] = useState<Notice>(launch.target ? { text: "Проверяем ссылку из Telegram…" } : null);

  useEffect(() => {
    if (!launch.target) return;
    let cancelled = false;
    const run = async () => {
      try {
        await Promise.resolve();
        if (cancelled) return;
        if (launch.invalid) throw new Error(launch.invalid);
        const supported: LaunchTarget[] = ["questionnaire", "applications", "resume", "audit", "accounts", "settings"];
        if (!supported.includes(launch.target as LaunchTarget)) throw new Error("Неизвестный раздел в ссылке из Telegram.");
        const target = launch.target as LaunchTarget;
        let accountId = launch.accountId;
        let snapshotId = launch.snapshotId;

        if (target === "questionnaire" || (target === "applications" && launch.applyId)) {
          if (!launch.applyId) throw new Error("В ссылке на анкету отсутствует apply_id.");
          const questionnaire = await client.fetchQuery({ queryKey: ["questionnaire-direct", launch.applyId], queryFn: () => api.questionnaire(launch.applyId!) });
          if (accountId && accountId !== questionnaire.account_id) throw new Error("Анкета не относится к указанному аккаунту.");
          accountId = questionnaire.account_id;
          client.setQueryData(["questionnaire", accountId, launch.applyId], questionnaire);
        }

        if ((target === "resume" || target === "audit") && snapshotId) {
          const candidates = accountId ? initialDashboard.accounts.filter((item) => item.id === accountId) : initialDashboard.accounts;
          if (candidates.length === 0) throw new Error("Аккаунт для этого резюме недоступен.");
          const lists = await Promise.all(candidates.map(async (account) => ({
            account,
            resumes: await client.fetchQuery<Resume[]>({ queryKey: ["resumes", account.id], queryFn: () => api.resumes(account.id) }),
          })));
          const owner = lists.find((item) => item.resumes.some((resume) => resume.snapshot_id === snapshotId));
          if (!owner) throw new Error("Резюме из ссылки недоступно.");
          accountId = owner.account.id;
        }

        if (accountId) {
          if (!initialDashboard.accounts.some((item) => item.id === accountId)) throw new Error("Аккаунт из ссылки недоступен.");
          if (initialDashboard.active_account_id !== accountId) {
            await api.activateAccount(accountId);
            await client.invalidateQueries({ queryKey: ["dashboard"] });
          }
        }
        if (cancelled) return;
        setNotice(null);
        if (target === "questionnaire" || (target === "applications" && launch.applyId)) navigate(`/applications?apply_id=${launch.applyId}`, { replace: true });
        else if (target === "applications") navigate("/applications", { replace: true });
        else if (target === "resume") navigate(`/resumes${snapshotId ? `?snapshot_id=${snapshotId}` : ""}`, { replace: true });
        else if (target === "audit") navigate(`/resumes?focus=audit${snapshotId ? `&snapshot_id=${snapshotId}` : ""}`, { replace: true });
        else navigate("/settings", { replace: true });
      } catch (error) {
        if (!cancelled) setNotice({ text: errorMessage(error), error: true });
      }
    };
    void run();
    return () => { cancelled = true; };
  }, [client, initialDashboard, launch, navigate]);

  return <Message notice={notice} />;
}

function Root() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [accessError, setAccessError] = useState<ApiError | null>(null);
  const entered = useRef(false);
  const authAttempted = useRef(false);
  useEffect(() => {
    const revoked = (event: Event) => {
      const error = (event as CustomEvent<ApiError>).detail;
      if (error.code === "FORBIDDEN") {
        void client.cancelQueries({ queryKey: ["admin"] });
        client.removeQueries({ queryKey: ["admin"] });
        navigate("/settings", { replace: true });
        void client.invalidateQueries({ queryKey: ["dashboard"] });
      } else if (error.status === 403 || entered.current) {
        setAccessError(error);
        setCsrfToken("");
        void client.cancelQueries();
        client.clear();
      }
    };
    window.addEventListener("leadscout-access", revoked);
    return () => window.removeEventListener("leadscout-access", revoked);
  }, [client, navigate]);
  const dashboard = useQuery({
    queryKey: ["dashboard"],
    queryFn: async () => {
      try {
        const data = await api.dashboard();
        setCsrfToken(data.csrf_token);
        entered.current = true;
        return data;
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 401 || entered.current || authAttempted.current) throw error;
        authAttempted.current = true;
        await authenticate();
        const data = await api.dashboard();
        setCsrfToken(data.csrf_token);
        entered.current = true;
        return data;
      }
    },
    enabled: !accessError,
    retry: false,
    refetchInterval: () => !accessError && document.visibilityState === "visible" ? 15000 : false,
  });
  if (accessError) return <div className={styles.loading}><h1>LeadScout</h1><Message notice={{ text: accessError.message, error: true }} /><p>{accessError.code === "ACCESS_BLOCKED" ? "Данные кабинета скрыты. Обратитесь к главному администратору." : "Закройте и заново откройте Mini App для входа с актуальными правами."}</p></div>;
  if (dashboard.isPending) return <div className={styles.loading} role="status"><strong>LeadScout</strong>Загружаем кабинет…</div>;
  if (dashboard.isError || !dashboard.data) return <div className={styles.loading}><h1>LeadScout</h1><Message notice={{ text: `Не удалось открыть кабинет. ${dashboard.error instanceof Error ? dashboard.error.message : ""}`, error: true }} /><Button onClick={() => { void dashboard.refetch(); }} disabled={dashboard.isFetching}>Повторить</Button></div>;
  const accountKey = dashboard.data.active_account_id ?? "none";

  return <AppLayout dashboard={dashboard.data}>
    <DirectLinkController dashboard={dashboard.data} />
    <Routes>
      <Route path="/" element={<HomePage dashboard={dashboard.data} key={`home:${accountKey}`} />} />
      <Route path="/applications" element={<ApplicationsPage dashboard={dashboard.data} key={`applications:${accountKey}`} />} />
      <Route path="/resumes" element={<ResumesPage dashboard={dashboard.data} key={`resumes:${accountKey}`} />} />
      <Route path="/admin" element={<AdminPage dashboard={dashboard.data} />} />
      <Route path="/settings" element={<SettingsPage dashboard={dashboard.data} />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  </AppLayout>;
}

export default function App() {
  return <HashRouter><Root /></HashRouter>;
}
