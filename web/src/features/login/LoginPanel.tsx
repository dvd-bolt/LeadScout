import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../shared/http/api";
import { ApiError } from "../../shared/http/client";
import { errorMessage } from "../../shared/lib/format";
import type { LoginFlowResponse, LoginFlowStatus } from "../../shared/types/api";
import { Button, Message, type Notice } from "../../shared/ui";
import styles from "../../shared/ui/UI.module.css";

type LoginStep = { status: LoginFlowStatus; accountId?: number; captcha?: string; message?: string; expiresIn?: number };

function isKnownStatus(status: string): status is LoginFlowStatus {
  return ["STARTING", "WAITING_FOR_CAPTCHA", "WAITING_FOR_OTP", "SUCCESS", "INVALID_CAPTCHA", "INVALID_CODE", "ERROR"].includes(status);
}

export function LoginPanel() {
  const client = useQueryClient();
  const [login, setLogin] = useState("");
  const [code, setCode] = useState("");
  const [step, setStep] = useState<LoginStep | null>(() => {
    try { return JSON.parse(sessionStorage.getItem("leadscout:login-step") || "null") as LoginStep | null; }
    catch { return null; }
  });
  const [notice, setNotice] = useState<Notice>(null);
  const activeFlow = useQuery({
    queryKey: ["login-flow", "active"], queryFn: api.activeLogin, retry: false,
    refetchInterval: (query) => ["STARTING", "WAITING_FOR_CAPTCHA", "WAITING_FOR_OTP"].includes(String(query.state.data?.status)) ? 3000 : false,
  });

  const handleResult = (data: LoginFlowResponse) => {
    const status = data.status;
    if (!isKnownStatus(status)) {
      setStep(null);
      setCode("");
      setNotice({ text: "Получен неизвестный ответ сервера. Начните вход заново.", error: true });
      return;
    }
    if (status === "SUCCESS") {
      setStep(null);
      setCode("");
      setNotice({ text: "Вход выполнен" });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
      return;
    }
    if (status === "ERROR" || status === "INVALID_CODE") {
      setStep(null);
      setCode("");
      setNotice({ text: data.message || "Начните вход заново", error: true });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
      return;
    }
    setStep((previous) => ({
      status,
      accountId: data.account_id ?? previous?.accountId,
      captcha: data.captcha_data_uri ?? previous?.captcha,
      message: data.message,
      expiresIn: data.expires_in,
    }));
    if (status === "WAITING_FOR_OTP") {
      setCode("");
      setNotice({ text: "hh.ru принял запрос кода. Доставка SMS может занять несколько минут." });
    } else if (status === "INVALID_CAPTCHA") {
      setCode("");
      setNotice({ text: data.message || "Капча не принята. Введите текст с новой картинки.", error: true });
    } else {
      setNotice(data.message ? { text: data.message } : null);
    }
  };
  useEffect(() => {
    if (activeFlow.data?.status === "NONE") {
      setStep(null);
      setCode("");
    } else if (activeFlow.data && (!step || activeFlow.data.status !== step.status)) {
      handleResult(activeFlow.data);
    }
    // handleResult intentionally reads only local setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeFlow.data]);
  const handleError = (error: unknown) => {
    if (error instanceof ApiError && error.status === 409 && step) {
      setStep(null);
      setCode("");
      setNotice({ text: "Сохранённый шаг входа больше не активен. Начните вход заново.", error: true });
      return;
    }
    setNotice({ text: errorMessage(error), error: true });
  };
  const start = useMutation({ mutationFn: () => api.startLogin(login), onSuccess: handleResult, onError: handleError });
  const submit = useMutation({
    mutationFn: () => step?.status === "WAITING_FOR_CAPTCHA" || step?.status === "INVALID_CAPTCHA" ? api.submitCaptcha(code, step.accountId) : api.submitOtp(code, step?.accountId),
    onSuccess: handleResult,
    onError: handleError,
  });
  const reload = useMutation({ mutationFn: () => api.reloadCaptcha(step?.accountId), onSuccess: handleResult, onError: handleError });
  const language = useMutation({ mutationFn: () => api.switchCaptchaLanguage(step?.accountId), onSuccess: handleResult, onError: handleError });
  const cancel = useMutation({
    mutationFn: () => api.cancelLogin(step?.accountId),
    onSuccess: () => {
      setStep(null);
      setCode("");
      setNotice({ text: "Вход отменён" });
      void client.invalidateQueries({ queryKey: ["dashboard"] });
    },
    onError: handleError,
  });
  const formBusy = start.isPending || submit.isPending || reload.isPending || language.isPending || cancel.isPending;
  const waitingForCaptcha = step?.status === "WAITING_FOR_CAPTCHA" || step?.status === "INVALID_CAPTCHA";
  useEffect(() => {
    if (step) sessionStorage.setItem("leadscout:login-step", JSON.stringify(step));
    else sessionStorage.removeItem("leadscout:login-step");
  }, [step]);

  return <section className={styles.card}>
    <h2>Подключить или обновить вход</h2>
    <p className={styles.meta}>Повторный вход использует тот же аккаунт и сохраняет его историю.</p>
    <Message notice={notice} />
    {step?.status === "STARTING" ? <div className={styles.notice}>hh.ru ещё обрабатывает начало входа. Эта форма обновится автоматически.</div> : !step ? <form className={styles.form} onSubmit={(event) => { event.preventDefault(); setNotice(null); start.mutate(); }}>
      <label className={styles.field}>Телефон или email hh.ru<input required value={login} onChange={(event) => setLogin(event.target.value)} /></label>
      <Button disabled={formBusy} type="submit">{start.isPending ? "Связываемся с hh.ru…" : "Продолжить"}</Button>
    </form> : <form className={styles.form} onSubmit={(event) => { event.preventDefault(); setNotice(null); submit.mutate(); }}>
      {step.captcha ? <img alt="Капча hh.ru" src={step.captcha} style={{ maxWidth: "100%", borderRadius: 12 }} /> : null}
      <label className={styles.field}>{waitingForCaptcha ? "Текст с картинки" : "Код из SMS или письма"}<input required value={code} inputMode={waitingForCaptcha ? "text" : "numeric"} autoComplete={waitingForCaptcha ? "off" : "one-time-code"} minLength={waitingForCaptcha ? 1 : 4} maxLength={waitingForCaptcha ? 32 : 8} pattern={waitingForCaptcha ? undefined : "[0-9]{4,8}"} onChange={(event) => setCode(event.target.value)} /></label>
      <div className={styles.actionRow}>
        <Button disabled={formBusy} type="submit">{submit.isPending ? "Проверяем…" : "Подтвердить"}</Button>
        {waitingForCaptcha ? <>
          <Button className={styles.secondary} disabled={formBusy} type="button" onClick={() => reload.mutate()}>Обновить капчу</Button>
          <Button className={styles.secondary} disabled={formBusy} type="button" onClick={() => language.mutate()}>Сменить язык</Button>
        </> : null}
        <Button className={styles.secondary} disabled={formBusy} type="button" onClick={() => cancel.mutate()}>Отменить</Button>
      </div>
      {step.message ? <div className={styles.meta}>{step.message}</div> : null}
      {typeof step.expiresIn === "number" ? <div className={styles.meta}>Шаг входа доступен ещё примерно {Math.max(1, Math.ceil(step.expiresIn / 60))} мин.</div> : null}
    </form>}
  </section>;
}
