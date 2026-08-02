# 📄 Отчет о Реализации Мульти-аккаунтности (Implementation Report)

В данном документе детально описано, **что** и **как** было реализовано в проекте **LeadScout AI** для поддержки нескольких аккаунтов **hh.ru** с параллельным запуском 2 браузеров одновременно на 1 IP-адресе.

---

## 📌 1. Что было сделано (Overview)

1. **Мульти-аккаунтность для соискателя:**
   - Один пользователь Telegram может подключать 2, 3, 5 или более аккаунтов hh.ru.
   - Каждый аккаунт имеет **свои независимые зашифрованные сессии (куки/LocalStorage)**, свое резюме (PDF/текст), ключевые слова, стоп-слова, минимальную ЗП, прокси и суточный лимит.
2. **Параллельная работа 2 браузеров на 1 IP:**
   - Настроен глобальный асинхронный семафор `asyncio.Semaphore(2)` в воркере.
   - Реализована **случайная стартовая рассинхронизация (Staggering delay: 5–15 сек)** перед запуском 2-го аккаунта, предотвращающая блокировки на 1 IP.
3. **Безопасная смена аккаунта в Telegram UI:**
   - Кнопка **`🔄 Сменить аккаунт`** выводит список аккаунтов со статусами.
   - Переключение аккаунта меняет контекст редактирования в Telegram **без разлогина и без удаления сохраненных сессий и куков**.
   - Добавлены массовые кнопки `🚀 Запустить ВСЕ`, `⛔️ Остановить ВСЕ` и индивидуальное подтверждение `🗑 Удалить аккаунт`.

---

## 📐 2. Как это реализовано (Техническая Архитектурная декомпозиция)

### A. База данных SQLite (`database.py`)
- **Таблица `hh_accounts`**: создана для хранения мульти-профилей соискателя со следующей структурой:
  - `id` (INTEGER PRIMARY KEY)
  - `user_id` (INTEGER, ссылка на владельца Telegram)
  - `account_name` (название профиля, например: `+79991112233` или `Python Dev`)
  - `encrypted_storage_state` (BLOB, зашифрованные Fernet AES-256 куки)
  - `session_status` (`ACTIVE | NOT_AUTHORIZED | EXPIRED`)
  - `resume_text`, `active_resume_url`, `active_resume_title`
  - `keywords`, `stop_words`, `min_salary`, `only_remote`, `proxy_url`
  - `daily_limit`, `applied_today`, `auto_apply_enabled`, `send_cover_letter`
- **Миграция существующих таблиц**:
  - В `users` добавлено поле `active_account_id INTEGER`.
  - В `hh_applies` и `pending_questionnaires` добавлено поле `account_id INTEGER`.
- **Новые функции БД**:
  - `get_user_accounts(user_id)` — получить список аккаунтов.
  - `get_active_account(user_id)` & `set_active_account(user_id, account_id)` — безопасное переключение active аккаунта.
  - `create_hh_account(user_id, phone_or_email)` — создание нового профиля.
  - `update_account_settings(account_id, **kwargs)` & `update_account_session(account_id, bytes, status)` — изоляция настроек.
  - `delete_hh_account(account_id)` — физическое удаление профиля.
  - `reset_all_account_daily_limits()` — суточный сброс откликов.

---

### B. Воркер и Планировщик (`worker.py` & `scheduler_app.py`)
- **Семафор и параллельность (`worker.py`)**:
  ```python
  browser_semaphore = asyncio.Semaphore(MAX_CONCURRENT_BROWSERS) # (по умолчанию 2)
  ```
- **Защита на 1 IP (Staggering)**:
  Внутри `process_account_hh_applications(account_id)` при старте выполняется:
  ```python
  async with browser_semaphore:
      stagger_delay = random.uniform(5.0, 15.0)
      await asyncio.sleep(stagger_delay) # задержка перед вторым браузером
  ```
- **Планировщик (`scheduler_app.py`)**:
  Каждые 45 минут выбирает все аккаунты из `hh_accounts WHERE session_status = 'ACTIVE' AND auto_apply_enabled = 1` и отправляет задачи поиска откликов.

---

### C. Telegram UI & Управление (`handlers.py` & `keyboards.py`)
- **Новые клавиатуры (`keyboards.py`)**:
  - `get_accounts_inline_keyboard(accounts, active_account_id)` — инлайн-список аккаунтов с иконками статусов (`🟢 [АКТИВЕН]`, `🚀 ВКЛЮЧЕН`).
  - `get_delete_confirmation_keyboard(account_id)` — диалог подтверждения удаления.
  - `get_main_keyboard` — добавлена кнопка `👤 Мои аккаунты`.
- **Обработчики (`handlers.py`)**:
  - `cmd_accounts` / `F.text == "👤 Мои аккаунты"` — вывод списка аккаунтов.
  - Callback `select_acc_{acc_id}` — переключает active_account_id без сброса куков.
  - Callback `switch_account_menu` — мгновенно открывает список аккаунтов.
  - Callback `add_new_account` / `🔑 Авторизация hh.ru` — создает аккаунт в БД и напускает 2FA логин.
  - Callback `start_all_accounts` & `stop_all_accounts` — массовое включение/выключение.
  - Callback `confirm_delete_acc_{acc_id}` & `delete_acc_{acc_id}` — удаление аккаунта.

---

## 🗂 3. Описание всех измененных файлов

| Файл | Описание изменений |
| :--- | :--- |
| **[config.py](file:///c:/Users/dvd/Desktop/LeadScout_AI/config.py)** | Добавлены константы `MAX_CONCURRENT_BROWSERS = 2` и `MAX_ACCOUNTS_PER_USER = 5`. |
| **[database.py](file:///c:/Users/dvd/Desktop/LeadScout_AI/database.py)** | Создана таблица `hh_accounts`, миграции полей, добавлено 12 асинхронных функций мульти-аккаунтности. |
| **[parsers/hh_login.py](file:///c:/Users/dvd/Desktop/LeadScout_AI/parsers/hh_login.py)** | `HHLoginSession` принимает `account_id` и сохраняет зашифрованную сессию в профиль аккаунта. |
| **[parsers/hh_resume.py](file:///c:/Users/dvd/Desktop/LeadScout_AI/parsers/hh_resume.py)** | `HHResumeManager` считывает сессию и загружает PDF под сессией активного аккаунта. |
| **[worker.py](file:///c:/Users/dvd/Desktop/LeadScout_AI/worker.py)** | Добавлен `asyncio.Semaphore(2)`, пауза 5-15 сек на 1 IP, логирование с указанием имени аккаунта. |
| **[scheduler_app.py](file:///c:/Users/dvd/Desktop/LeadScout_AI/scheduler_app.py)** | Автопоиск отправляет задачи параллельно всем активным мульти-аккаунтам из БД. |
| **[keyboards.py](file:///c:/Users/dvd/Desktop/LeadScout_AI/keyboards.py)** | Созданы клавиатуры вывода аккаунтов, смены профилей, подтверждения удаления. |
| **[handlers.py](file:///c:/Users/dvd/Desktop/LeadScout_AI/handlers.py)** | Добавлены обработчики `👤 Мои аккаунты`, смены аккаунта без разлогина, удаления и настроек. |

---

## 💡 4. Инструкция пользования в Telegram-боте

1. **Добавление аккаунта:**
   - Нажмите `👤 Мои аккаунты` ➔ `➕ Добавить новый аккаунт` (или `🔑 Авторизация hh.ru`).
   - Введите номер телефона / email ➔ Введите капчу при наличии ➔ Введите 4-значный СМС-код.
   - Аккаунт сохранен!
2. **Смена активного аккаунта для настройки:**
   - Нажмите `👤 Мои аккаунты` ➔ Выберите любой аккаунт (например, `+79994445566`).
   - Нажмите `⚙️ Настройки` или `📄 Мое резюме` — теперь вы редактируете параметры именно этого аккаунта. **Сессия первого аккаунта НЕ сбрасывается!**
3. **Запуск откликов:**
   - Нажмите `🚀 Запустить ВСЕ` в меню аккаунтов или `🚀 Запустить автоотклик` в главном меню.
   - Бот параллельно поднимет до 2 браузеров со сдвигом 5-15 сек и начнет высылать уведомления в чат.

---

## 🧪 5. Результаты верификации

- **Компиляция синтаксиса:** `python -m py_compile` для всех модулей — **0 ошибок**.
- **Тест подключения:** импорт всех файлов — **Успешно**.
- **Автоматический тест БД (`test_multi_account_db.py`):**
  - Создание аккаунтов ➔ **ОК**
  - Бесшовное переключение active_account ➔ **ОК**
  - Изоляция настроек ➔ **ОК**
  - Безопасное удаление ➔ **ОК**
