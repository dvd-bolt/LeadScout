import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { errorMessage } from "../../shared/lib/format";
import type { LoginFlowResponse } from "../../shared/types/api";
import { Button, Message, type Notice } from "../../shared/ui";
import styles from "../../shared/ui/UI.module.css";

type LoginStep = { status: string; captcha?: string; message?: string };

export function LoginPanel() {
  const client = useQueryClient();
  const [login, setLogin] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<LoginStep | null>(null);
  const [notice, setNotice] = useState<Notice>(null);

  const handleResult = (data: LoginFlowResponse) => {
    client.invalidateQueries({ queryKey: ["dashboard"] });
    setCode("");
    if (["ERROR", "INVALID_CODE"].includes(data.status)) {
      setStep(null);
      setNotice({ text: data.message || "Начните вход заново", error: true });
      return;
    }
    const status = data.status === "INVALID_CAPTCHA" ? "WAITING_FOR_CAPTCHA" : data.status;
    setStep((previous) => ({ status, captcha: data.captcha_data_uri ?? previous?.captcha, message: data.message }));
    setNotice(data.status === "SUCCESS"
      ? { text: "Вход выполнен" }
      : data.message ? { text: data.message, error: data.status === "INVALID_CAPTCHA" } : null);
  };
  const handleError = (error: unknown) => setNotice({ text: errorMessage(error), error: true });
  const start = useMutation({ mutationFn: () => api.startLogin(login), onSuccess: handleResult, onError: handleError });
  const submit = useMutation({
    mutationFn: () => step?.status === "WAITING_FOR_CAPTCHA" ? api.submitCaptcha(code) : api.submitOtp(code),
    onSuccess: handleResult,
    onError: handleError,
  });
  const reload = useMutation({ mutationFn: api.reloadCaptcha, onSuccess: handleResult, onError: handleError });
  const language = useMutation({ mutationFn: api.switchCaptchaLanguage, onSuccess: handleResult, onError: handleError });
  const cancel = useMutation({
    mutationFn: api.cancelLogin,
    onSuccess: () => { setStep(null); setCode(""); setNotice({ text: "Вход отменён" }); },
    onError: handleError,
  });
  const captchaBusy = submit.isPending || reload.isPending || language.isPending || cancel.isPending;

  return <section className={styles.card}>
    <h2>Подключить или обновить вход</h2>
    <p className={styles.meta}>Повторный вход использует тот же аккаунт и сохраняет его историю.</p>
    <Message notice={notice} />
    {!step || step.status === "SUCCESS" ? <form className={styles.form} onSubmit={(event) => { event.preventDefault(); setNotice(null); start.mutate(); }}>
      <label className={styles.field}>Телефон или email hh.ru<input required value={login} onChange={(event) => setLogin(event.target.value)} /></label>
      <Button disabled={start.isPending} type="submit">Продолжить</Button>
    </form> : <form className={styles.form} onSubmit={(event) => { event.preventDefault(); setNotice(null); submit.mutate(); }}>
      {step.captcha ? <img alt="Капча hh.ru" src={step.captcha} style={{ maxWidth: "100%", borderRadius: 12 }} /> : null}
      <label className={styles.field}>{step.status === "WAITING_FOR_CAPTCHA" ? "Текст с картинки" : "Код из SMS"}<input required value={code} onChange={(event) => setCode(event.target.value)} /></label>
      <div className={styles.actionRow}>
        <Button disabled={captchaBusy} type="submit">Подтвердить</Button>
        {step.status === "WAITING_FOR_CAPTCHA" ? <>
          <Button className={styles.secondary} disabled={captchaBusy} type="button" onClick={() => reload.mutate()}>Обновить капчу</Button>
          <Button className={styles.secondary} disabled={captchaBusy} type="button" onClick={() => language.mutate()}>Сменить язык</Button>
        </> : null}
        <Button className={styles.secondary} disabled={captchaBusy} type="button" onClick={() => cancel.mutate()}>Отменить</Button>
      </div>
      {step.message ? <div className={styles.meta}>{step.message}</div> : null}
    </form>}
  </section>;
}
