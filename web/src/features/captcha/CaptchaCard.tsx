import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { errorMessage } from "../../shared/lib/format";
import type { ActiveCaptcha, Dashboard } from "../../shared/types/api";
import { Button, Message, type Notice } from "../../shared/ui";
import { Icon } from "../../shared/ui/Icon";
import styles from "../../shared/ui/UI.module.css";
import local from "./CaptchaCard.module.css";

export function CaptchaCard({ captcha }: { captcha: ActiveCaptcha }) {
  const client = useQueryClient();
  const [code, setCode] = useState("");
  const [notice, setNotice] = useState<Notice>(null);
  const [currentUri, setCurrentUri] = useState(captcha.captcha_data_uri);
  const [dismissed, setDismissed] = useState(false);

  const submit = useMutation({
    mutationFn: () => api.submitAutomationCaptcha(captcha.account_id, code),
    onSuccess: async (data) => {
      if (data.status === "SUCCESS") {
        setDismissed(true);
        setCode("");
        client.setQueryData<Dashboard>(["dashboard"], (old) => {
          if (!old) return old;
          return {
            ...old,
            active_captcha: undefined,
            accounts: old.accounts.map((acc) =>
              acc.id === captcha.account_id
                ? {
                    ...acc,
                    pending_captcha_data_uri: "",
                    pending_captcha_created_at: "",
                    automation_state: acc.auto_apply_enabled ? "SEARCHING" : "WAITING",
                  }
                : acc
            ),
          };
        });
        await client.refetchQueries({ queryKey: ["dashboard"] });
      } else if (data.status === "INVALID_CAPTCHA") {
        if (data.captcha_data_uri) setCurrentUri(data.captcha_data_uri);
        setCode("");
        setNotice({ error: true, text: data.message || "Неверный код. Попробуйте еще раз." });
      } else {
        setNotice({ error: true, text: data.message || "Не удалось отправить капчу." });
      }
    },
    onError: (error) => setNotice({ error: true, text: errorMessage(error) }),
  });

  const reload = useMutation({
    mutationFn: () => api.reloadAutomationCaptcha(captcha.account_id),
    onSuccess: (data) => {
      if (data.captcha_data_uri) {
        setCurrentUri(data.captcha_data_uri);
        setCode("");
        setNotice({ tone: "success", text: "Картинка капчи обновлена." });
      }
    },
    onError: (error) => setNotice({ error: true, text: errorMessage(error) }),
  });

  const busy = submit.isPending || reload.isPending;

  const isStale = (() => {
    if (!captcha.created_at) return false;
    try {
      const created = new Date(captcha.created_at.replace(" ", "T") + "Z").getTime();
      return Date.now() - created > 10 * 60 * 1000;
    } catch {
      return false;
    }
  })();

  if (dismissed) return null;

  return (
    <section className={`${styles.card} ${local.captchaCard}`} aria-label="Требуется ввод капчи hh.ru">
      <div className={local.header}>
        <div className={local.titleRow}>
          <Icon name="alert" size={24} />
          <h2>hh.ru запросил ввод капчи</h2>
        </div>
        <p className={styles.meta}>
          Для продолжения автооткликов подтвердите, что вы не робот. Введите текст с картинки ниже:
        </p>
      </div>

      <Message notice={notice} />

      <form
        className={styles.form}
        onSubmit={(event) => {
          event.preventDefault();
          if (!code.trim()) return;
          setNotice(null);
          submit.mutate();
        }}
      >
        {isStale ? (
          <p className={local.staleHint}>
            💡 Капча получена более 10 минут назад. Если при отправке возникнет ошибка, просто нажмите «Обновить картинку».
          </p>
        ) : null}

        <div className={local.imageContainer}>
          {currentUri ? (
            <img alt="Капча hh.ru" src={currentUri} className={local.captchaImg} />
          ) : (
            <div className={styles.meta}>Загрузка картинки…</div>
          )}
        </div>

        <label className={styles.field}>
          Текст с картинки
          <input
            required
            autoFocus
            value={code}
            disabled={busy}
            inputMode="text"
            autoComplete="off"
            placeholder="Введите символы…"
            onChange={(event) => setCode(event.target.value)}
          />
        </label>

        <div className={styles.actionRow}>
          <Button disabled={busy || !code.trim()} type="submit">
            {submit.isPending ? "Проверяем на hh.ru…" : "Отправить капчу"}
          </Button>
          <Button
            className={styles.secondary}
            disabled={busy}
            type="button"
            onClick={() => reload.mutate()}
          >
            {reload.isPending ? "Обновляем…" : "Обновить картинку"}
          </Button>
        </div>
      </form>
    </section>
  );
}
