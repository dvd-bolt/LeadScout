# Модуль 03 — мультиаккаунты

Status: VERIFIED_LOCAL.

## Доказанные гарантии

- Все account-scoped SQLite чтения/изменения используют владельца; чужие account,
  resume и questionnaire ID возвращают недоступность. Revision анкеты остаётся
  обязательной, повторное подтверждение не запускает вторую отправку.
- Session state/cookies, resume text, дневной счётчик, history/events и уведомления
  изолированы для 2 пользователей с 2 аккаунтами у каждого. Совпадающий vacancy ID
  не объединяет лимиты либо историю разных аккаунтов.
- Два `AppContext` с отдельными SQLite и одинаковыми numeric ID не разделяют
  locks, browser pool, AI cache либо зашифрованные session state.
- Pool переиспользует browser engine только для одинакового нормализованного proxy;
  два account-flow получают разные BrowserContext и не делят авторизованный context.
- Login registry/cleanup/timers и locks используют `(user_id, account_id)`.
  Новый login того же аккаунта заменяет только его сессию, а login другого аккаунта
  того же пользователя продолжает работу. UI передаёт account ID через OTP/CAPTCHA.
- Поиск, анкета, sync/resume и login одного аккаунта сериализуются account-lock.
  Управляемый тест удерживает lock, отменяет ожидающую анкету только целевого
  аккаунта, оставляет второй search работающим и подтверждает последующий restart.
  Смена active account после старта не меняет захваченный ID задачи.
- Проверки: 62 профильных storage/access/revision/lifecycle test passed;
  `tests/test_tasks_and_browser.py` — 12 passed; Ruff профильных файлов и compileall.

## Live-граница

- Не выполнялись реальный hh.ru login/OTP/CAPTCHA, отправка отклика, Telegram и
  подключение к рабочему proxy. Внешние browser/form services были заменены;
  SQLite, coordinator, locks и browser-pool boundary были настоящими.

## Финальная перепроверка

- Login-задача получает account ID уже при регистрации, а не после завершения.
- Полная owner/account матрица и lifecycle вошли в зелёный полный прогон:
  276 passed; Ruff и compileall также успешны.
