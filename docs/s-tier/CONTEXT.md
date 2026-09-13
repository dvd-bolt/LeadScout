# LeadScout S-tier context

## Назначение

LeadScout — Telegram Mini App и фоновый worker для поиска вакансий hh.ru,
подготовки и подтверждённой отправки откликов. S-tier выполняется последовательно:
1. диагностика откликов; 2. текст резюме; 3. мультиаккаунты;
4. парсер вакансий; 5. сквозная надёжность откликов.

## Фактическая архитектура

- `server_app.py` собирает FastAPI, bot, scheduler и worker в одном процессе.
- `leadscout/runtime/` содержит `AppContext`, coordinator и внедряемые зависимости.
- `leadscout/jobs/search.py` запускает поиск и отклики по аккаунту;
  `jobs/questionnaire.py` отправляет подтверждённые анкеты.
- `leadscout/integrations/application_forms.py` — граница Patchright/hh.ru.
- `leadscout/storage/` — SQLite, миграции и scoped-репозитории;
  `applications.py` ведёт `hh_applies` и итоговые `application_events`.
- `leadscout/api/routes/applications.py` и `web/src/shared/ui/EventRow.tsx`
  показывают существующую историю без отдельной админ-панели.

## Локальные команды Windows

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\patchright.exe install chromium
npm.cmd --prefix web ci
npm.cmd --prefix web run build
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\ruff.exe check leadscout tests
```

## Общие правила S-tier

- Сначала читать этот файл и `STATE.md`, затем только нужный код и отчёты.
- Расширять механизмы с публичной совместимостью; не ослаблять тесты.
- Воспроизводить дефект, исправлять и проверять регрессию на временной SQLite БД
  и подменённых внешних отправках.
- Промежуточные этапы — только в безопасном ротируемом локальном логе;
  `application_events` содержит лишь один итог попытки и не завышает статистику.
- Не запускать рабочую автоматизацию, не менять меню bot и не отправлять реальные отклики.
- Не выводить в логи/отчёты секреты, сессии, резюме, письма и тексты анкет.
- `VERIFIED_LOCAL` означает выполненные локальные критерии; live-проверка отмечается отдельно.
- После модуля обновить `STATE.md` и отчёт не длиннее 60 строк. Не переходить дальше.
