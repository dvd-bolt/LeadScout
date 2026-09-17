import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { QuestionnaireList } from "../features/questionnaires/QuestionnaireList";
import { api } from "../shared/http/api";
import { EventRow } from "../shared/ui/EventRow";
import type { Dashboard } from "../shared/types/api";
import { Message } from "../shared/ui";
import styles from "../shared/ui/UI.module.css";

function formatSearchRun(status: string) {
  if (status === "SUCCESS") return "Поиск выполнен";
  if (status === "WARNING") return "Поиск выполнен с предупреждениями";
  return "Поиск не выполнен";
}

export function ApplicationsPage({ dashboard }: { dashboard: Dashboard }) {
  const queryClient = useQueryClient();
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
  const history = useInfiniteQuery({
    queryKey: ["applications", accountId ?? "none"],
    queryFn: ({ pageParam }) => api.applications(accountId, pageParam, 50),
    initialPageParam: undefined as number | undefined,
    getNextPageParam: (lastPage) => lastPage.history.length === 50
      ? lastPage.history[lastPage.history.length - 1]?.id
      : undefined,
    enabled: tab === "history" && Boolean(accountId),
    refetchInterval: tab === "history" && document.visibilityState === "visible" ? 15000 : false,
    refetchOnWindowFocus: "always",
  });
  const historyEvents = (() => {
    const review = history.data?.pages[0]?.review_required ?? [];
    const ordinary = history.data?.pages.flatMap((page) => page.history) ?? [];
    return [...review, ...ordinary].filter((event, index, all) => all.findIndex((item) => item.id === event.id) === index);
  })();
  const resolution = useMutation({
    mutationFn: ({ attemptId, applied }: { attemptId: string; applied: boolean }) => api.resolveApplication(attemptId, applied),
    onSuccess: async () => {
      await Promise.all([
        history.refetch(),
        questionnaires.refetch(),
        directApplyId ? direct.refetch() : Promise.resolve(),
        queryClient.invalidateQueries({ queryKey: ["dashboard"] }),
      ]);
    },
  });
  const deleteHistory = useMutation({
    mutationFn: api.deleteApplicationHistory,
    onSuccess: async () => {
      await history.refetch();
      await queryClient.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
  const exportHistory = async () => {
    const data = await api.exportApplicationHistory();
    const href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = href;
    link.download = `leadscout-history-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(href);
  };
  const loading = directApplyId ? direct.isPending : Boolean(accountId) && (tab === "review" ? questionnaires.isPending : history.isPending);
  const error = resolution.isError ? resolution.error : direct.isError ? direct.error : questionnaires.isError ? questionnaires.error : history.isError ? history.error : null;

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
        : <section className={styles.card}><div className={styles.cardHeader}><div><h2>История откликов</h2><p className={styles.meta}>Подробная история хранится 180 дней. ID отправленных откликов сохраняются для защиты от дублей.</p></div><div className={styles.actionRow}><button className={styles.textButton} onClick={() => void exportHistory()}>Экспорт JSON</button><button className={`${styles.textButton} ${styles.danger}`} disabled={deleteHistory.isPending} onClick={() => { if (window.confirm("Удалить историю, письма и диагностику? Защита от повторных откликов сохранится.")) deleteHistory.mutate(); }}>Удалить историю</button></div></div>
          {(history.data?.pages[0]?.search_runs?.length ?? 0) > 0 ? <details><summary>Циклы поиска</summary>{history.data!.pages[0].search_runs.map((run) => <div className={styles.meta} key={run.id}>{formatSearchRun(run.status)} · найдено {run.found}, подтверждённых откликов {run.processed} · {new Date(run.created_at).toLocaleString("ru-RU")}{run.details ? ` · ${run.details}` : ""}</div>)}</details> : null}
          <div className={styles.eventList} style={{ marginTop: 14 }}>
          {historyEvents.length ? historyEvents.map((event) => <EventRow key={event.id} event={event}
            resolving={resolution.isPending && resolution.variables?.attemptId === event.attempt_id}
            onResolve={event.attempt_id ? (applied) => resolution.mutate({ attemptId: event.attempt_id!, applied }) : undefined}
          />) : <div className={styles.empty}>Пока пусто.</div>}
          {history.hasNextPage ? <button className={styles.textButton} disabled={history.isFetchingNextPage} onClick={() => void history.fetchNextPage()}>{history.isFetchingNextPage ? "Загружаем…" : "Показать ещё"}</button> : null}
        </div></section>)}
  </section>;
}
