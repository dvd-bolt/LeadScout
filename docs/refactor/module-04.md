# LeadScout: модуль 04 — API, бот и интеграция

Protocol: parallel-v1
Status: COMPLETE

Модуль 04 реализует разделённый API, компактный Telegram-бот, общий runtime,
точки запуска, интеграционные тесты и финальное подключение модулей 01–03.

Модули 01, 02 и 03 переданы со статусом `READY` и подключены в общей интеграции.

## Реализовано

- Монолитная HTTP-поверхность разделена на `leadscout/api`: assembly приложения,
  Telegram/cookie auth, зависимости, request schemas, public presenters и группы routes.
- Все бизнес-последовательности API подключены к реальному `leadscout.services` через
  внедряемый `AppContext`; простые scoped-чтения используют стабильный DB-фасад.
- Добавлены чтение и идемпотентный пропуск анкеты, массовый запуск, смена языка CAPTCHA,
  строгий text/URL match и независимый аудит без hh.ru-аккаунта.
- Реестр операций вынесен в `leadscout/runtime/operations.py`. `NEEDS_FIELDS` сохраняется
  как терминальный `NEEDS_INPUT`; бизнес-ошибки получают `FAILED`, задачи дедуплицируются,
  отменяются и восстанавливаются без автоматического повтора.
- PDF импорта создаётся только внутри выполняемой операции и удаляется при любом выходе;
  извлечение PDF-текста выполняется вне event loop.
- Создан `leadscout/bot`: компактные команды входа/помощи/остановки, выбор аккаунта,
  Mini App deep-links и безопасные legacy-переходы. Уведомления остаются на общем
  notifier/formatter модуля 02.
- Созданы общий runtime context, lifecycle и scheduler; точки запуска используют общий
  coordinator, login manager, browser pool, AI client и блокировки.
- `web_api.py` и `scheduler_app.py` оставлены совместимыми фасадами. Старые корневые
  bot-модули сохранены для импортной совместимости, но точки запуска подключают новый router.
- Добавлены интеграционные тесты с настоящими FastAPI/SQLite и подменами только внешних
  hh.ru/Gemini/Telegram границ.

## Проверки

- `npm --prefix web run build` — успешно, 106 модулей.
- `.venv/bin/ruff check .` — успешно.
- `.venv/bin/python -m pytest -q` — `121 passed in 106.05s`.
- Browser suite — production React + настоящий FastAPI + временная SQLite + Chromium.
- `.venv/bin/python -m pip check` — `No broken requirements found`.
- `.venv/bin/python -m compileall ...` — успешно.
- `git diff --check` — успешно.

Docker CLI в текущем окружении отсутствует, поэтому container build/config не запускались.
Живые действия hh.ru, Gemini, Telegram, BotFather и deployment намеренно не выполнялись.
