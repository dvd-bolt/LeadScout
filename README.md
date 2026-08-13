# LeadScout AI

Локальный Telegram-бот для работы с аккаунтами соискателя hh.ru: синхронизации резюме,
поиска вакансий, отправки откликов, заполнения анкет и ATS-аудита через Google Gemini.

## Возможности

- Несколько аккаунтов hh.ru с отдельными сессиями, настройками, прокси и лимитами.
- Один локальный диспетчер задач с ограничением числа Chromium-контекстов и защитой от повторного запуска.
- Проверка дублей и атомарная фиксация подтвержденных откликов в SQLite.
- Сопроводительные письма и ответы работодателю через Gemini Structured Output.
- Автоотправка анкеты только при уверенности не ниже `0.85` и валидных обязательных ответах.
- Синхронизация резюме по стабильным ID, безопасная загрузка PDF и подтверждение результата на hh.ru.
- ATS-аудит и PDF-отчет с экранированием содержимого модели.

При выключенном сопроводительном письме в соответствующее поле намеренно отправляется `.`.
Текст загруженного PDF передается в Google Gemini для анализа. Сам PDF хранится только во
временном каталоге на локальном компьютере и удаляется после операции.

## Требования

- Windows 10/11.
- Python 3.13, доступный через launcher `py`.
- Telegram Bot Token и Google Gemini API key.

Redis, Taskiq, Docker, PostgreSQL и системный Google Chrome не требуются.

## Установка

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\patchright.exe install chromium
```

Создайте `.env` на основе `.env.example`. Fernet-ключ генерируется так:

```powershell
.\.venv\Scripts\python.exe -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Обязательные переменные:

```env
BOT_TOKEN=telegram_bot_token
GEMINI_API_KEY=google_gemini_api_key
GEMINI_MODEL=gemini-3.5-flash-lite
SESSION_ENCRYPTION_KEY=generated_fernet_key
```

Запуск:

```powershell
.\.venv\Scripts\python.exe main.py
```

## Проверка

```powershell
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q .
.\.venv\Scripts\python.exe -m pip check
```

Тесты используют временную SQLite-базу, локальные HTML-фикстуры и Chromium. Они не
отправляют реальные отклики работодателям.

## Архитектура

- `main.py`: lifecycle Telegram, scheduler, Gemini и браузеров.
- `handlers.py`: команды, callback-проверки владельца и FSM-диалоги.
- `worker.py`: `TaskCoordinator`, account locks и очередь браузеров.
- `database.py`: SQLite schema v3, миграции, ownership и атомарные транзакции.
- `ai_handler.py`: один асинхронный Gemini-клиент, Pydantic-схемы и bounded TTL cache.
- `parsers/`: авторизация, Chromium pool, резюме и отклики hh.ru.
- `utils/`: Fernet, валидация URL/прокси, PDF и humanized browser controls.

SQLite работает в WAL-режиме. Foreign keys включаются на каждом соединении. Диалоги FSM
хранятся в памяти и после перезапуска начинаются заново; аккаунты, сессии и история остаются
в `leadscout.db`.

## Эксплуатационные Ограничения

- DOM hh.ru может изменяться; успех операции всегда дополнительно проверяется по видимому состоянию страницы.
- Сессия с неверным Fernet-ключом помечается истекшей и требует повторного входа.
- Live-проверку отправки отклика выполняйте отдельно и осознанно: финальная кнопка создает реальное действие для работодателя.
