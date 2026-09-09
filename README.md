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
- FastAPI API под `/api/v1`, журнал длительных операций и интерфейс React/Vite с нижней
  навигацией, темой Telegram, безопасными отступами и кнопкой «Назад».
- Авторизация Mini App проверяет подпись и срок `initData`, принимает только
  `OWNER_TELEGRAM_ID`, создаёт короткую HttpOnly-сессию и требует Origin + CSRF для изменений.
- Аудит активного, отдельного текстового или PDF-резюме; сравнение с вакансией и PDF-отчёт.

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
OWNER_TELEGRAM_ID=ваш_числовой_telegram_id
APP_URL=https://leadscout.example.com
WEB_APP_ORIGINS=https://leadscout.example.com
WEB_SECURE_COOKIES=true
```

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

## Развёртывание на VPS

1. Скопируйте проект, создайте `.env` по примеру и задайте настоящий `DOMAIN`, `APP_URL`,
   `WEB_APP_ORIGINS`, `OWNER_TELEGRAM_ID`, токены и исходный `SESSION_ENCRYPTION_KEY`.
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
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q .
.venv/bin/python -m pip check
cd web && npm run build
```

Тесты используют временную SQLite-базу и локальные браузерные формы. Они не выполняют
реальные входы или отклики на hh.ru. Перед живой отправкой выполняйте отдельную проверку с
явным разрешением владельца.

## Структура

- `server_app.py` — единый процесс Uvicorn, Telegram polling и APScheduler.
- `web_api.py` — авторизованный HTTP API и журнал длительных операций.
- `web/` — React + TypeScript + Vite Mini App.
- `worker.py` — задачи аккаунтов, блокировки и отправка анкет.
- `database.py` — SQLite WAL, миграции, ownership и снимки резюме.
- `parsers/` — безопасные сценарии hh.ru и PDF.
- `backup.py`, `Dockerfile`, `docker-compose.yml`, `Caddyfile` — развёртывание и резервные копии.
