import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { request } from "../shared/http/client";
import { Button, Card, Message } from "../shared/ui";
import type { Dashboard } from "../shared/types/api";
import { formatDate } from "../shared/lib/format";
import { People } from "../features/admin/People";
import { Tasks } from "../features/admin/Tasks";
import { Journal } from "../features/admin/Journal";
import { title, useVisible, type Overview as OverviewData } from "../features/admin/model";
import styles from "../features/admin/Admin.module.css";
import ui from "../shared/ui/UI.module.css";

function Overview() {
  const visible = useVisible();
  const overview = useQuery({
    queryKey: ["admin", "overview"],
    queryFn: () => request<OverviewData>("/admin/overview"),
    enabled: visible,
    refetchInterval: visible ? 15000 : false,
    retry: false,
  });
  const data = overview.data;
  const aiLabel = data
    ? ({
        UNKNOWN: "Сведений пока нет",
        STALE: "Данные устарели",
        OK: "Последнее обращение успешно",
        ERROR: "Последнее обращение завершилось ошибкой",
      })[data.ai.status] ?? data.ai.status
    : "";
  const notice = overview.error
    ? { text: overview.error.message, error: true }
    : overview.isPending
      ? { text: "Проверяем состояние…" }
      : null;

  return (
    <div className={styles.stack}>
      <Button className={ui.secondary} disabled={overview.isFetching} onClick={() => void overview.refetch()}>
        Обновить состояние
      </Button>
      <Message notice={notice} />
      {data ? (
        <>
          <div className={styles.grid}>
            <Card>
              <h3>API</h3>
              <p>Процесс работает</p>
              <div className={styles.meta}>
                Запуск: {formatDate(data.api.started_at)} МСК<br />
                Время работы: {Math.floor(data.api.uptime_seconds / 60)} мин.
              </div>
            </Card>
            <Card>
              <h3>База данных</h3>
              <p>{data.database.ok ? "Чтение работает" : "Ошибка чтения"}</p>
            </Card>
            <Card>
              <h3>Бот</h3>
              <p>{data.bot.polling ? "Polling выполняется" : "Polling не выполняется"}</p>
              <p className={styles.meta}>Состояние задачи не гарантирует доступность Telegram.</p>
            </Card>
            <Card>
              <h3>Планировщик</h3>
              <p>{data.scheduler.running ? "Запущен" : "Остановлен"}</p>
              <p className={styles.meta}>
                Следующий поиск: {formatDate(data.scheduler.next_run_at)}
                {data.scheduler.next_run_at ? " МСК" : ""}
              </p>
            </Card>
          </div>
          <Card>
            <h3>ИИ</h3>
            <p>{aiLabel}</p>
            <p className={styles.meta}>
              Последние сведения: {formatDate(data.ai.checked_at)}
              {data.ai.checked_at ? " МСК" : ""}. Открытие раздела не запускает генерацию.
            </p>
          </Card>
          <Card>
            <h3>Задания сейчас</h3>
            <div className={styles.grid}>
              {["QUEUED", "RUNNING", "WAITING_INPUT", "STOPPING"].map((state) => (
                <div key={state}>
                  <div className={styles.number}>{data.tasks[state] ?? 0}</div>
                  {title(state)}
                </div>
              ))}
            </div>
          </Card>
          <Card>
            <h3>Ошибки за сутки</h3>
            <div className={styles.number}>{data.errors_24h}</div>
            <Link to="/admin?tab=journal">Открыть журнал ошибок</Link>
          </Card>
        </>
      ) : null}
    </div>
  );
}

export function AdminPage({ dashboard }: { dashboard: Dashboard }) {
  const [params, setParams] = useSearchParams();
  if (dashboard.role !== "ROOT" && dashboard.role !== "ADMIN") {
    return <Message notice={{ text: "Недостаточно прав.", error: true }} />;
  }
  const tabs = [
    { id: "overview", label: "Обзор" },
    ...(dashboard.role === "ROOT" ? [{ id: "people", label: "Люди" }] : []),
    { id: "tasks", label: "Задания" },
    { id: "journal", label: "Журнал" },
  ];
  const tab = params.get("tab") ?? "overview";
  const content = tab === "people"
    ? dashboard.role === "ROOT"
      ? <People />
      : <Message notice={{ text: "Недостаточно прав.", error: true }} />
    : tab === "tasks"
      ? <Tasks role={dashboard.role} />
      : tab === "journal"
        ? <Journal />
        : <Overview />;

  return (
    <section className={ui.page}>
      <Link to="/settings">← Настройки</Link>
      <h1 className={`${ui.pageTitle} ${styles.heading}`}>Администрирование</h1>
      <p className={styles.meta}>
        {dashboard.role === "ROOT"
          ? "Управление доступом, системой и заданиями"
          : "Диагностика и остановка заданий. Главный администратор защищён."}
      </p>
      <nav className={styles.tabs} aria-label="Разделы администрирования">
        {tabs.map((item) => (
          <Button
            key={item.id}
            className={tab === item.id ? "" : ui.secondary}
            aria-current={tab === item.id ? "page" : undefined}
            onClick={() => setParams({ tab: item.id })}
          >
            {item.label}
          </Button>
        ))}
      </nav>
      {content}
    </section>
  );
}
