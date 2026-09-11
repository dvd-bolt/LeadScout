Protocol: parallel-v1
Status: READY

# Модуль 03 — frontend LeadScout

## Структура

- `web/src/app`: корневой `App`, `HashRouter`, провайдер React Query, Telegram BackButton и layout.
- `web/src/pages`: `HomePage`, `ApplicationsPage`, `ResumesPage`, `SettingsPage`.
- `web/src/features`: `accounts`, `login`, `automation`, `questionnaires`, `resumes`, `audits`.
- `web/src/shared`: HTTP-клиент, API-методы, Telegram SDK, общие UI-компоненты, типы и форматирование.
- Старые точки импорта `App.tsx`, `api.ts`, `telegram.ts`, `types.ts` оставлены тонкими совместимыми реэкспортами.

## Реализовано

- Сохранены четыре раздела, мобильное оформление, `HashRouter`, Telegram theme params и BackButton.
- Все account-scoped запросы и polling операций имеют `account_id`/account scope в query key; страницы с формами перемонтируются при смене аккаунта.
- Анкеты: сохранение, подтверждение, пропуск, обновление списка/dashboard и получение актуального состояния после `409`. Обработанная анкета из direct link показывается без повторного действия.
- Автоматизация: запуск/остановка одного аккаунта, остановка всех и `start-all` с отдельной строкой результата по каждому аккаунту. Частичный результат не показывается как общий успех; неизвестные статусы имеют fallback-текст.
- Login flow: повторная авторизация, неверная CAPTCHA без выхода из сценария, reload, переключение языка и cancel.
- Импорт PDF: один активный submit, polling до terminal state, отдельные успех/ошибка/`NEEDS_INPUT`. Исходный `File` хранится только в памяти компонента; после явного подтверждения отправляется новый multipart с тем же `file` и дополненным `structured_json`. Автоматического повтора нет.
- Аудит доступен и без hh.ru-аккаунта: отдельный текст и PDF, подробные `penalties`, `top_recommendations`, `insights` (`tier`, `title`, `description`, `score_impact`).
- Сравнение аудита переключается между текстом и hh.ru URL и отправляет ровно одно из полей `vacancy_text`/`vacancy_url`.
- Direct links сначала проверяют анкету/резюме/аккаунт через API, затем при необходимости активируют подтверждённый аккаунт. Недоступный объект не переключает активный аккаунт и не запускает никаких предметных действий.
- Production HTTP-клиент не содержит моков и не обращается к `localStorage`/`sessionStorage`.

## Используемые HTTP-контракты

Все пути имеют префикс `/api/v1`; все изменяющие запросы используют `X-CSRF-Token`, полученный из `GET /me`/Telegram auth.

- `GET /questionnaires?account_id=…`, `GET /questionnaires/{apply_id}`, `PATCH /questionnaires/{apply_id}`, `POST /questionnaires/{apply_id}/confirm`, `POST /questionnaires/{apply_id}/skip`.
- `POST /automation/{account_id}/start`, `POST /automation/{account_id}/stop`, `POST /automation/start-all`, `POST /automation/stop-all`.
- `POST /login-flows/start`, `/otp`, `/captcha`, `/captcha/reload`, `/captcha/language`, `/cancel`.
- `GET /accounts/{account_id}/resumes`, sync/activate/delete и multipart `POST /accounts/{account_id}/resumes/import` с `file` и необязательным `structured_json`.
- `GET /audits[?account_id=…]`, `POST /audits`, multipart `POST /audits/pdf`, `POST /audits/{audit_id}/match` с union payload.
- `GET /operations/{operation_id}`; `PENDING`/`RUNNING` продолжают polling, `SUCCEEDED`/`FAILED`/`NEEDS_INPUT` его останавливают. Для миграционного поведения UI также распознаёт `SUCCEEDED` + `result.status=NEEDS_FIELDS`.

## Формат прямых ссылок для чата 4

Канонический формат для Telegram: query-параметры исходного URL располагаются **до** hash fragment:

```text
https://<mini-app-host>/?target=questionnaire&apply_id=123&account_id=7#/
https://<mini-app-host>/?target=applications&account_id=7#/
https://<mini-app-host>/?target=applications&apply_id=123&account_id=7#/
https://<mini-app-host>/?target=resume&account_id=7&snapshot_id=55#/
https://<mini-app-host>/?target=audit&account_id=7&snapshot_id=55#/
https://<mini-app-host>/?target=accounts&account_id=7#/
https://<mini-app-host>/?target=settings&account_id=7#/
```

Правила:

- Все идентификаторы — положительные целые числа.
- Для `questionnaire` обязателен `apply_id`; `account_id` необязателен, но при наличии должен совпасть с аккаунтом анкеты.
- `applications` с `apply_id` открывает конкретную анкету; без него — список выбранного/активного аккаунта.
- Для `resume`/`audit` `snapshot_id` необязателен. При наличии резюме проверяется через `GET /accounts/{account_id}/resumes`; если `account_id` не передан, поиск идёт только среди доступных пользователю аккаунтов.
- `audit` без аккаунта поддержан и открывает независимый аудит.
- Также принимаются параметры внутри hash-route (`#/?target=…`), но Telegram следует генерировать канонический вариант выше.
- Direct link выполняет только безопасные GET-проверки и, после них, активацию доступного аккаунта. Он не подтверждает/не пропускает анкету, не удаляет резюме и не запускает автоматизацию или аудит.

## Проверки

- `npm --prefix web run build` — успешно (`tsc -b`, Vite 6.4.3, 106 modules transformed).
- `PYTHONPATH=. ./.venv/bin/ruff check tests/parallel_ui` — успешно.
- `PYTHONPATH=. ./.venv/bin/pytest -q tests/parallel_ui` — `6 passed`.
- Browser suite использует собранный `web/dist`, настоящий headless Chromium, мобильный viewport `390×844`, уникальные временные static roots и persistent browser profiles. `/api/v1/**` перехватывается ответами по согласованным контрактам; внешних запросов к hh.ru/Gemini/Telegram нет.
- Проверены четыре страницы, отсутствие горизонтального overflow, account isolation, настройки, повторный login, invalid/reload/language CAPTCHA, questionnaire save/confirm/skip/409, частичный start-all, импорт success/error/NEEDS_INPUT/continuation/no-auto-retry, независимый аудит, подробности, text/URL match payloads, direct links, ошибки и пустые состояния.

## Требует проверки с настоящим API

- Реальная Telegram session/cookie/CSRF связка и Telegram BackButton/theme events.
- Интеграция новых backend-маршрутов из `docs/refactor/requests/03-to-04.md`.
- Реальное продвижение долгих операций и сохранение результатов после ответа worker.
- Реальные multipart limits/validation и мастер дозаполнения hh.ru.
- Загрузка PDF-отчёта в авторизованной браузерной сессии.

Все перечисленные browser-проверки выполнены с перехватом HTTP. Полный end-to-end с настоящим backend этим модулем не заявляется.
