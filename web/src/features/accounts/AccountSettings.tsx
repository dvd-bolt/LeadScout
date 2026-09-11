import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../../shared/http/api";
import { errorMessage } from "../../shared/lib/format";
import type { Account } from "../../shared/types/api";
import { Button, Field, Message, type Notice } from "../../shared/ui";
import styles from "../../shared/ui/UI.module.css";

export function AccountSettings({ account }: { account: Account }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [notice, setNotice] = useState<Notice>(null);
  const update = useMutation({
    mutationFn: (form: FormData) => {
      const proxyUrl = String(form.get("proxy_url") || "").trim();
      return api.patchAccount(account.id, {
        account_name: String(form.get("account_name")),
        keywords: String(form.get("keywords")),
        stop_words: String(form.get("stop_words")),
        min_salary: Number(form.get("min_salary") || 0),
        daily_limit: Number(form.get("daily_limit")),
        only_remote: form.get("only_remote") === "on",
        send_cover_letter: form.get("send_cover_letter") === "on",
        ...(form.get("remove_proxy") === "on" ? { proxy_url: "" } : proxyUrl ? { proxy_url: proxyUrl } : {}),
      });
    },
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["dashboard"] });
      setNotice({ tone: "success", text: "Настройки сохранены" });
    },
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteAccount(account.id),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["dashboard"] });
      navigate("/");
    },
    onError: (error) => setNotice({ text: errorMessage(error), error: true }),
  });

  return <section className={styles.card}>
    <div className={styles.cardHeader}><h2>Настройки поиска</h2><span className={styles.status}>{account.has_proxy ? "Прокси задан" : "Без прокси"}</span></div>
    <Message notice={notice} />
    <form className={styles.form} onSubmit={(event) => { event.preventDefault(); update.mutate(new FormData(event.currentTarget)); }}>
      <Field label="Название аккаунта"><input defaultValue={account.account_name} name="account_name" /></Field>
      <label className={styles.field}>Ключевые слова<input defaultValue={account.keywords} name="keywords" placeholder="Python, backend" /></label>
      <label className={styles.field}>Стоп-слова<input defaultValue={account.stop_words} name="stop_words" placeholder="стажёр, продажи" /></label>
      <div className={styles.actionRow}>
        <label className={styles.field}>Мин. зарплата<input defaultValue={account.min_salary || ""} min="0" name="min_salary" type="number" /></label>
        <label className={styles.field}>Лимит в день<input defaultValue={account.daily_limit} min="1" max="200" name="daily_limit" type="number" /></label>
      </div>
      <label className={styles.switch}><input defaultChecked={account.only_remote} name="only_remote" type="checkbox" /> Только удалённая работа</label>
      <label className={styles.switch}><input defaultChecked={account.send_cover_letter} name="send_cover_letter" type="checkbox" /> Добавлять сопроводительное письмо</label>
      <details><summary>Дополнительные параметры</summary>
        <label className={styles.field} style={{ marginTop: 10 }}>Прокси URL<input name="proxy_url" placeholder={account.has_proxy ? "Прокси задан — вставь новый URL для замены" : "http://user:password@host:port"} /></label>
        {account.has_proxy ? <label className={styles.switch}><input type="checkbox" name="remove_proxy" /> Удалить прокси</label> : null}
      </details>
      <Button disabled={update.isPending} type="submit">Сохранить настройки</Button>
    </form>
    <Button className={`${styles.secondary} ${styles.dangerButton}`} disabled={remove.isPending} onClick={() => {
      if (window.confirm(`Удалить аккаунт «${account.account_name}» и его локальные данные?`)) remove.mutate();
    }}>Удалить аккаунт</Button>
  </section>;
}
