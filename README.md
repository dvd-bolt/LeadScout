# LeadScout

LeadScout — личный кабинет в Telegram для работы с аккаунтами соискателя hh.ru.
Бот остаётся входом в приложение, каналом уведомлений и аварийной остановкой, а аккаунты,
резюме, поиск, отклики, анкеты и ИИ-аудит доступны в Telegram Mini App.

## Что реализовано

- Несколько изолированных аккаунтов hh.ru: повторный вход использует существующий аккаунт и
  не стирает историю. Сессии и реквизиты прокси никогда не возвращаются HTTP-клиенту.
- Резюме синхронизируются по стабильному ID hh.ru. Запуск невозможен без явно выбранного
  резюме, а его удаление требует подтверждения в интерфейсе и подтверждённого результата на
  hh.ru.
- Автоматизация использует один `TaskCoordinator`, блокировки аккаунтов и общий лимит
  Chromium-контекстов. Повторные запуски и подтверждения анкет не создают вторую задачу.
- Снимок исходного резюме хранится с анкетой и ИИ-аудитом. При перезапуске незавершённая
  отправка переводится в «Требуется проверка», без слепой повторной отправки.
- Разделённый FastAPI API под `/api/v1`, журнал длительных операций и интерфейс React/Vite с нижней
  навигацией, темой Telegram, безопасными отступами и кнопкой «Назад».
- Авторизация Mini App проверяет подпись и срок `initData`, принимает только
  список `OWNER_TELEGRAM_IDS`, создаёт короткую HttpOnly-сессию и требует Origin + CSRF для изменений.
- Аудит активного, отдельного текстового или PDF-резюме; сравнение с вакансией и PDF-отчёт.
- Компактный бот оставляет `/start`, помощь, уведомления и прямую остановку. Старые команды
  и callback-кнопки открывают соответствующий экран проверки в Mini App и сами не выполняют
  запуск, удаление либо отправку.
- Импорт, которому не хватает полей, завершается терминальным состоянием `NEEDS_INPUT`.
  Продолжение создаётся явным новым запросом с тем же PDF и дополненным JSON без скрытого повтора.
- Неполную анкету можно сохранить как черновик. Подтверждение требует обязательных ответов
  и совпадения версии; во время отправки интерфейс обновляется каждые 3 секунды.
- SQLite v6 исправляет старые телефонные ключи. При коллизиях миграция останавливается
  без изменения данных и сообщает ID спорных аккаунтов.

## Локальная разработка

Нужны Python 3.13+, Node.js 22+ и токены Telegram/Gemini.

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/patchright install chromium

cd web
npm ci
npm run build
cd ..
```

Создайте `.env` из `.env.example`. Fernet-ключ:

```bash
.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Для одного процесса с ботом, планировщиком и Mini App нужны также:

```env
OWNER_TELEGRAM_IDS=первый_числовой_id,второй_числовой_id
APP_URL=https://leadscout.example.com
WEB_APP_ORIGINS=https://leadscout.example.com
WEB_SECURE_COOKIES=true
```

Старое поле `OWNER_TELEGRAM_ID` поддерживается, если список `OWNER_TELEGRAM_IDS` пуст.
Список имеет приоритет: аккаунты и история каждого владельца остаются изолированными.

Запустите серверный режим:

```bash
.venv/bin/python server_app.py
```

Для разработки UI отдельно:

```bash
cd web && npm run dev
```

Локальная вкладка без Telegram ожидаемо покажет ошибку входа: приложение принимает только
подписанные данные Mini App. Для продуктивного запуска укажите HTTPS-домен в BotFather как
URL Mini App. Кнопка «Открыть LeadScout» появится в боте автоматически, когда `APP_URL`
начинается с `https://`.

Для временного HTTPS без собственного домена см. [локальный запуск через Cloudflare](docs/refactor/LOCAL_LAUNCH_2026-09-11.md).
После смены `APP_URL` команда `.venv/bin/python -m scripts.set_mini_app_menu` обновляет меню бота.

## Развёртывание на VPS

1. Скопируйте проект, создайте `.env` по примеру и задайте настоящий `DOMAIN`, `APP_URL`,
   `WEB_APP_ORIGINS`, `OWNER_TELEGRAM_IDS`, токены и исходный `SESSION_ENCRYPTION_KEY`.
2. До миграции сохраните существующую базу и Fernet-ключ. Ключ нужен для расшифровки
   сохранённых сессий hh.ru.
3. Запустите:

   ```bash
   docker compose up -d --build
   ```

`docker-compose.yml` запускает один worker приложения, Caddy с HTTPS и ежедневную
консистентную SQLite-копию. База находится в томе `leadscout-data`, последние 14 копий — в
`leadscout-backups`. Для проверки копии восстановите её в отдельный файл через SQLite
`backup` API, затем запустите приложение с временным `DB_PATH`.

После настройки домена задайте тот же HTTPS URL в BotFather для Menu Button/Mini App.

## Проверки

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
npm --prefix web ci
npm --prefix web run build
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q -x '/(\.venv|node_modules|scratch|\.git)/' .
.venv/bin/python -m pip check
```

Тесты используют временную SQLite-базу, локальные браузерные формы и собранный Mini App
с настоящим FastAPI API. Браузерные тесты Mini App пропускаются, если `web/dist` не собран,
поэтому сборку нужно выполнить до `pytest`. Тесты не выполняют
реальные входы или отклики на hh.ru. Перед живой отправкой выполняйте отдельную проверку с
явным разрешением владельца.

Результаты и ограничения: [повторная проверка от 11.09.2026](docs/refactor/SECOND_REVIEW_2026-09-11.md).

Контейнерная проверка в окружении с Docker: `sh scripts/verify-container.sh`. Она использует
отдельный Compose, временную БД и заглушки внешних отправок; рабочие тома и `.env` не нужны.

## Архитектура

- `server_app.py` — единый процесс Uvicorn, Telegram polling и APScheduler.
- `leadscout/runtime/` — единый `AppContext`, lifecycle, планировщик, реестр операций,
  координатор и общие блокировки.
- `leadscout/api/` — сборка приложения, auth/dependencies/schemas и независимые группы routes;
  `web_api.py` оставлен тонким совместимым фасадом.
- `leadscout/bot/` — компактные handlers/keyboards/router и безопасные legacy-переходы;
  корневые `handlers.py` и `keyboards.py` сохранены для совместимости старых импортов.
- `leadscout/services/` — пять сервисов: аккаунты, резюме, автоматизация, анкеты и аудит;
  общие контракты, ошибки и сборка вынесены отдельно.
- `leadscout/storage/` — SQLite-соединение, миграции и репозитории с ownership scope.
- `web/` — React + TypeScript + Vite Mini App.
- `leadscout/jobs/`, `leadscout/integrations/`, `leadscout/documents/` — задания,
  внешние границы hh.ru/Gemini и PDF; корневые фасады продолжают поддерживать старые импорты.
- `backup.py`, `Dockerfile`, `docker-compose.yml`, `Caddyfile` — развёртывание и резервные копии.

Архитектура, история модулей и проверки: [docs/refactor/README.md](docs/refactor/README.md).
