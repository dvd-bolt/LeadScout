# 📌 План внедрения всех оставшихся этапов оптимизации LeadScout AI

Настоящий план описывает пошаговое внедрение всех оставшихся незавершенных этапов архитектурной и технической оптимизации системы **LeadScout AI**.

---

## 🗺 Карта этапов внедрения

```
                                  REMAINING IMPLEMENTATION ROADMAP
┌──────────────────────────────┐   ┌──────────────────────────────┐   ┌──────────────────────────────┐
│  ЭТАП 1: CONTEXT POOLING     │──>│  ЭТАП 2: REDIS FSM & CACHE   │──>│  ЭТАП 3: FASTAPI WEBHOOKS    │
│  • Пул браузера Patchright   │   │  • RedisStorage для FSM      │   │  • HTTP POST вебхуки         │
│  • Синглтон Chromium в памяти│   │  • Кэш ИИ-аудитов в Redis    │   │  • Nginx Reverse Proxy       │
└──────────────────────────────┘   └──────────────────────────────┘   └──────────────────────────────┘
                                                                                      │
                                                                                      ▼
┌──────────────────────────────┐   ┌──────────────────────────────┐   ┌──────────────────────────────┐
│  ГОТОВО К ПРОДАКШЕНУ         │<──│  ЭТАП 5: POSTGRESQL (ASYNCPG)│<──│  ЭТАП 4: LOCAL BOT API DOCKER│
│  • Нагрузка до 100 000 польз.│   │  • Миграция СУБД на Postgres │   │  • Прямая передача file://   │
│  • Задержка < 5 мс           │   │  • Снятие лимитов транзакций │   │  • Файлы до 2 ГБ без лимитов │
└──────────────────────────────┘   └──────────────────────────────┘   └──────────────────────────────┘
```

---

## 📝 Детализация всех нереализованных этапов

### 🟢 ЭТАП 1: Внедрение пула браузерных контекстов Patchright (`parsers/hh_browser.py`)
* **Что нужно сделать:**
  1. Преобразовать `HHBrowserEngine` в синглтон-сервис, поддерживающий постоянно запущенный единственный инстанс Chromium браузера в памяти приложения.
  2. Переписать вызовы `upload_pdf_resume_to_hh`, `fetch_user_resumes` и `process_account_hh_applications`, чтобы они вызывали только быстрое создание изолированного контекста:
     ```python
     context = await engine.get_or_create_context(storage_state=state)
     ```
* **Ожидаемый результат:** Ускорение работы автоотклика и сканирования hh.ru в **3–5 раз**, снижение потребления RAM на **90%**.

---

### 🟢 ЭТАП 2: Перенос FSM состояний и кэша ИИ в Redis (`main.py`, `config.py`, `ai_handler.py`)
* **Что нужно сделать:**
  1. Подключить `redis-py` и настроить `RedisStorage` в `main.py`:
     ```python
     from aiogram.fsm.storage.redis import RedisStorage
     storage = RedisStorage.from_url(REDIS_URL)
     dp = Dispatcher(storage=storage)
     ```
  2. Добавить кэширование структурированных ответов Gemini AI по хэшу текста резюме в Redis: `key = f"ai_resume:{text_hash}"`.
* **Ожидаемый результат:** Мгновенный выбор FSM состояний диалога (< 1 мс), 100% сохранность контекстов пользователей при перезапуске бота, исключение повторных платных запросов к Gemini API.

---

### 🔵 ЭТАП 3: Переход на Webhooks через FastAPI & Nginx (`webhook_app.py`, `nginx.conf`)
* **Что нужно сделать:**
  1. Написать асинхронный веб-сервер `webhook_app.py` на базе FastAPI:
     ```python
     @app.post("/webhook")
     async def bot_webhook(request: Request):
         update = Update.model_validate(await request.json(), context={"bot": bot})
         await dp.feed_update(bot, update)
         return {"status": "ok"}
     ```
  2. Настроить конфигурацию Nginx с поддержкой SSL-сертификатов Let's Encrypt для проксирования запросов на FastAPI.
* **Ожидаемый результат:** Полный отказ от Long Polling, мгновенная реактивная доставка сообщений от Telegram API.

---

### 🔵 ЭТАП 4: Локальный Telegram Bot API Server в Docker (`docker-compose.yml`)
* **Что нужно сделать:**
  1. Создать `docker-compose.yml` с описанием контейнера `telegram-bot-api`:
     ```yaml
     services:
       telegram-bot-api:
         image: aiogram/telegram-bot-api:latest
         environment:
           TELEGRAM_API_ID: ${TELEGRAM_API_ID}
           TELEGRAM_API_HASH: ${TELEGRAM_API_HASH}
         ports:
           - "8081:8081"
     ```
  2. Переключить клиент Aiogram 3 на локальный сервер:
     ```python
     bot = Bot(token=BOT_TOKEN, session=AiohttpSession(api=TelegramAPIServer.from_base("http://localhost:8081")))
     ```
* **Ожидаемый результат:** Отклик Telegram API < 5 мс, прямая работа с тяжелыми PDF-файлами резюме до 2 ГБ через протокол `file://`.

---

### 🟣 ЭТАП 5: Миграция СУБД с SQLite на PostgreSQL (`database.py`, `models.py`)
* **Что нужно сделать:**
  1. Развернуть PostgreSQL и написать драйвер взаимодействия через `asyncpg` / `SQLAlchemy 2.0`.
  2. Написать миграционный скрипт переноса существующих аккаунтов и логов из `leadscout.db` в PostgreSQL.
* **Ожидаемый результат:** Полное снятие лимитов на количество параллельных транзакций и масштабируемость системы до сотни тысяч пользователей.

---

## 📊 Матрица приоритетов и трудозатрат

| Этап | Компонент | Сложность | Влияние на скорость | Приоритет |
|---|---|---|---|---|
| **Этап 1** | Browser Context Pooling | Средняя | ⚡️⚡️⚡️ (Ускорение в 5 раз) | **Критический** |
| **Этап 2** | Redis FSM & AI Cache | Низкая | ⚡️⚡️ (Сохранение FSM и экономия API) | **Высокий** |
| **Этап 3** | FastAPI Webhooks | Средняя | ⚡️⚡️ (Реактивность апдейтов) | **Высокий** |
| **Этап 4** | Local Bot API Server | Средняя | ⚡️⚡️ (Работа с файлами до 2 ГБ) | **Средний** |
| **Этап 5** | Asyncpg PostgreSQL | Высокая | ⚡️ (Масштаб от 10k пользователей) | **Средний** |
