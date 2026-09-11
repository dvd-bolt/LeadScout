import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { QuestionnaireList } from "../features/questionnaires/QuestionnaireList";
import { api } from "../shared/http/api";
import { EventRow } from "../shared/ui/EventRow";
import type { Dashboard } from "../shared/types/api";
import { Message } from "../shared/ui";
import styles from "../shared/ui/UI.module.css";

export function ApplicationsPage({ dashboard }: { dashboard: Dashboard }) {
  const accountId = dashboard.active_account_id ?? undefined;
  const [searchParams, setSearchParams] = useSearchParams();
  const directApplyId = Number(searchParams.get("apply_id")) || undefined;
  const tab = searchParams.get("tab") === "history" ? "history" : "review";
  const setTab = (value: "review" | "history") => {
    const next = new URLSearchParams(searchParams);
    next.set("tab", value);
    setSearchParams(next);
  };
  const questionnaires = useQuery({
    queryKey: ["questionnaires", accountId ?? "none"],
    queryFn: () => api.questionnaires(accountId!),
    enabled: Boolean(accountId),
    refetchInterval: (query) => document.visibilityState !== "visible" ? false
      : query.state.data?.some((item) => item.status === "SUBMITTING") ? 3000 : 15000,
    refetchOnWindowFocus: "always",
  });
  const direct = useQuery({
    queryKey: ["questionnaire", accountId ?? "none", directApplyId],
    queryFn: () => api.questionnaire(directApplyId!),
    enabled: Boolean(directApplyId),
    refetchOnWindowFocus: "always",
    refetchInterval: (query) => {
      if (document.visibilityState !== "visible") return false;
      const status = query.state.data?.status;
      if (status === "SUBMITTING") return 3000;
      if (!status || ["PENDING", "APPROVED", "FAILED", "NEEDS_REVIEW"].includes(status)) return 15000;
      return false;
    },
  });
  const history = useQuery({
    queryKey: ["applications", accountId ?? "none"],
    queryFn: () => api.applications(accountId),
    enabled: tab === "history" && Boolean(accountId),
  });
  const loading = directApplyId ? direct.isPending : Boolean(accountId) && (tab === "review" ? questionnaires.isPending : history.isPending);
  const error = direct.isError ? direct.error : questionnaires.isError ? questionnaires.error : history.isError ? history.error : null;

  return <section className={styles.page}>
    <h1 className={styles.pageTitle}>Отклики</h1>
    <Message notice={error ? { text: error.message, error: true } : null} />
    {directApplyId ? <div className={styles.directHeader}><strong>Анкета из уведомления</strong><button className={styles.textButton} onClick={() => { searchParams.delete("apply_id"); setSearchParams(searchParams, { replace: true }); }}>Показать весь список</button></div> : <div className={styles.tabs}>
      <button aria-pressed={tab === "review"} className={`${styles.tab} ${tab === "review" ? styles.tabActive : ""}`} onClick={() => setTab("review")}>Нужна проверка</button>
      <button aria-pressed={tab === "history"} className={`${styles.tab} ${tab === "history" ? styles.tabActive : ""}`} onClick={() => setTab("history")}>История</button>
    </div>}
    {loading ? <div role="status" className={styles.empty}>Загружаем отклики…</div> : null}
    {!loading && (directApplyId ? <QuestionnaireList items={direct.data ? [direct.data] : []} showProcessed />
      : tab === "review" ? <QuestionnaireList items={questionnaires.data ?? []} />
        : <section className={styles.card}><h2>История откликов</h2><div className={styles.eventList} style={{ marginTop: 14 }}>
          {history.data?.history.length ? history.data.history.map((event) => <EventRow key={event.id} event={event} />) : <div className={styles.empty}>Пока пусто.</div>}
        </div></section>)}
  </section>;
}
