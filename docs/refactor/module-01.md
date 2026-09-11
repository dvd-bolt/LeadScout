# LeadScout: модуль 01 — пакет и слой хранения

Protocol: parallel-v1
Status: READY

## Реализованные изменения

- Создан Python-пакет `leadscout` с ядром конфигурации и отдельным слоем SQLite-хранилища.
- Настройки перенесены в `leadscout/core/config.py` без изменения имён переменных окружения, значений по умолчанию и правил валидации.
- В `leadscout/core/paths.py` централизованы корень проекта, `.env`, `assets`, `web/dist` и стандартный путь базы. Явный `DB_PATH` из окружения по-прежнему имеет приоритет.
- Работа с соединением вынесена в `leadscout/storage/connection.py`, а схема и миграции v5 — в `leadscout/storage/migrations.py`.
- Запросы разделены по репозиториям пользователей, аккаунтов, резюме, откликов, анкет, аудитов и операций.
- Корневые `config.py` и `database.py` оставлены как временные оболочки совместимости. Реализация в них не дублируется.
- Тестовая фикстура теперь внедряет `Database` с временным файлом в фактическую точку использования. Отдельные подмены московской даты также выполняются в реальном модуле репозитория.
- Добавлена проверка прямого пакетного API, повторной инициализации базы и лимита аккаунтов.
- Добавлены согласованные экспорты `skip_pending_questionnaire` и `set_operation_needs_input`.

## Фактическая структура файлов модуля 01

```text
leadscout/
  __init__.py
  core/
    __init__.py
    config.py
    paths.py
  storage/
    __init__.py
    connection.py
    migrations.py
    repositories/
      __init__.py
      users.py
      accounts.py
      resumes.py
      applications.py
      questionnaires.py
      audits.py
      operations.py
config.py                 # оболочка совместимости
database.py               # оболочка совместимости
tests/
  conftest.py             # временная Database для каждого теста
  test_database.py        # схема, владение, конкуренция и прямой API
  parallel_storage/
    test_state_exports.py # новые межмодульные контракты и WAL-backup
```

В общей рабочей папке также присутствуют незавершённые файлы следующих модулей, созданные параллельно (`leadscout/integrations`, `leadscout/models`, `docs/refactor/module-02.md`, `docs/refactor/module-03.md`). Они не относятся к реализации модуля 01 и в рамках этой работы не изменялись.

## Публичный интерфейс

### Database и миграции

```python
from leadscout.storage import Database, SCHEMA_VERSION, init_db

database = Database(path, timeout=15.0)
await init_db(database)

async with database.connection() as connection:
    ...
```

`Database(path, *, timeout=15.0)` хранит явный `Path`. Методы `connection()` и совместимый алиас `connect()` возвращают асинхронный контекст соединения `aiosqlite` с `Row`-factory, включёнными foreign keys и busy timeout. `init_db(database)` создаёт новую базу либо обновляет существующую до `SCHEMA_VERSION == 5`; повторный вызов безопасен.

### Репозитории

Каждая публичная асинхронная функция ниже получает `Database` первым аргументом.

- `users`: `get_or_create_user`, `update_user_session`, `get_user_session`, `update_user_settings`.
- `accounts`: `get_user_accounts`, `get_enabled_accounts`, `set_next_scheduled_search_at`, `get_account_for_user`, `get_account_by_login`, `get_active_account`, `set_active_account`, `create_hh_account`, `update_account`, `update_account_settings_for_user`, `update_account_session`, `delete_hh_account_for_user`, `reset_all_account_daily_limits`; также исключения `AccountLimitError` и `DuplicateAccountError`.
- `resumes`: `sync_resume_snapshots`, `list_resume_snapshots`, `get_resume_snapshot_for_user`, `get_resume_snapshot_by_hh_id`, `get_active_resume_snapshot`, `attach_resume_text`, `set_active_resume_snapshot`, `delete_resume_snapshot`, `get_user_resumes_json`.
- `applications`: `is_account_already_applied`, `record_application_event`, `record_successful_application`, `is_already_applied`, `get_user_recent_applies`, `get_application_stats`, `list_application_events`.
- `questionnaires`: `list_pending_questionnaires`, `recover_interrupted_questionnaires`, `save_pending_questionnaire_account`, `save_pending_questionnaire`, `get_pending_questionnaire_for_user`, `claim_pending_questionnaire`, `skip_pending_questionnaire`, `finish_pending_questionnaire`, `update_pending_questionnaire_status`, `update_pending_questionnaire_letter`, `update_pending_questionnaire_answers`, `edit_pending_questionnaire`.
- `audits`: `save_resume_audit`, `get_user_latest_audit`, `list_resume_audits`, `get_resume_audit_for_user`.
- `operations`: `create_operation`, `recover_interrupted_operations`, `complete_operation`, `start_operation`, `set_operation_needs_input`, `get_operation_for_user`, `calculate_text_hash`.

Вспомогательные функции, которые должны участвовать в уже открытой транзакции, получают текущее `aiosqlite.Connection`; например, `accounts.reset_stale_account(connection, account_id)` не открывает отдельное соединение.

### Два согласованных экспорта

`questionnaires.skip_pending_questionnaire(database, user_id, apply_id) -> str` и совместимый `database.skip_pending_questionnaire(user_id, apply_id) -> str` возвращают:

- `SKIPPED` после атомарного перехода из `PENDING`, `FAILED` или `NEEDS_REVIEW`, а также при повторном вызове для уже пропущенной анкеты;
- `NOT_FOUND` для отсутствующей или чужой записи;
- `CONFLICT` для любого другого состояния, включая `APPROVED`, `SUBMITTING` и `SUBMITTED`.

Проверка состояния и перевод в `SKIPPED` выполняются под `BEGIN IMMEDIATE`, поэтому захват отправителем и пропуск сериализованы.

`operations.set_operation_needs_input(database, operation_id, user_id, result) -> bool` и совместимый `database.set_operation_needs_input(operation_id, user_id, result) -> bool` одним условным `UPDATE` переводят только принадлежащую пользователю `RUNNING`-операцию в `NEEDS_INPUT`, записывают JSON, очищают ошибку и обновляют время. `recover_interrupted_operations` по-прежнему изменяет только `PENDING` и `RUNNING`.

## Настройки и тестовая база

Основной модуль настроек: `leadscout.core.config`. Он загружает `PROJECT_ROOT/.env`. Стандартная база остаётся `PROJECT_ROOT/leadscout.db`; значение `DB_PATH` из окружения заменяет его. Пути к `assets`, `web/dist` и `browser_profiles` по-прежнему разрешаются относительно прежнего корня проекта.

Для тестов используется явное внедрение:

```python
test_database = Database(tmp_path / "leadscout-test.db")
monkeypatch.setattr(database_compat, "DEFAULT_DATABASE", test_database)
await init_db(test_database)
```

Новый код может передавать этот объект непосредственно функциям репозиториев. Старые потребители продолжают вызывать функции корневого `database.py` без аргумента базы.

## Сохранённые свойства данных и транзакций

- Схема и `PRAGMA user_version=5` сохранены, включая исторические таблицы и колонки.
- Успешный отклик, дневной счётчик и событие фиксируются одной `BEGIN IMMEDIATE`-транзакцией.
- Уникальность отклика по `(account_id, vacancy_hh_id)` сохранена и проверена конкурентной записью.
- Захват анкеты атомарно переводит только допустимое состояние в `SUBMITTING`.
- Одновременное редактирование письма и ответов выполняется одной транзакцией до захвата отправителем.
- Проверки владельца сохранены для аккаунтов, резюме, анкет и аудитов.
- Анкеты и аудиты сохраняют исходные данные резюме; последующее переключение или обновление активного резюме их не переписывает.
- Дневные правила используют московскую календарную дату; статистика переводит UTC timestamp SQLite в UTC+3.
- WAL-backup создаётся через SQLite backup API и восстанавливает зафиксированные данные.

## Оболочки совместимости

- `config.py` является алиасом модуля `leadscout.core.config`, поэтому старые импорты и подмены атрибутов работают с единственным источником настроек.
- `database.py` создаёт/принимает `DEFAULT_DATABASE` и делегирует все прежние функции соответствующим репозиториям. Старые сигнатуры сохранены, включая их представление через `inspect.signature`.
- `main.py` и `server_app.py` не меняли команд запуска и продолжают работать через совместимые импорты.

Стабильный публичный интерфейс корневого `database.py` включает прежние группы функций пользователей и аккаунтов, откликов и событий, анкет, резюме и аудитов, операций, `init_db`, `get_db_connection`, `Database`, `DB_PATH`, `DEFAULT_DATABASE`, `SCHEMA_VERSION`, `AccountLimitError` и `DuplicateAccountError`. К нему добавлены только два согласованных имени: `skip_pending_questionnaire` и `set_operation_needs_input`. Их совместимые сигнатуры проверяются через `inspect.signature`.

## Проверки и фактические результаты

Исходное состояние перед переносом:

- `.venv/bin/python -m pytest -q tests/test_database.py` — `6 passed`.
- `.venv/bin/ruff check config.py database.py leadscout` — успешно.
- `git diff --check` — успешно.

До перехода на параллельный протокол был выполнен общий прогон:

- Транзакционные, миграционные, конфигурационные и lifecycle-тесты — `49 passed`.
- `npm --prefix web run build` — успешно, Vite собрал `web/dist`.
- `.venv/bin/ruff check .` — успешно.
- `.venv/bin/python -m pytest -q` — `87 passed in 87.68s`, пропусков нет.
- `git diff --check` — успешно.

После получения `parallel-v1` общие проверки больше не запускались. Итоговые проверки только принадлежащей модулю 01 области:

- `.venv/bin/ruff check leadscout/__init__.py leadscout/core leadscout/storage config.py database.py backup.py tests/conftest.py tests/test_database.py tests/parallel_storage` — успешно.
- `.venv/bin/python -m pytest -q tests/test_database.py tests/parallel_storage -o cache_dir=.pytest_cache/module-01` — `17 passed in 0.36s`.
- `git diff --check` с ограничением на принадлежащие модулю 01 отслеживаемые файлы — успешно.

Проверены создание и повторная инициализация, старая схема, владение, лимит и дубли аккаунтов, конкурентный отклик, московская смена суток, неизменяемые снимки анкет и аудитов, атомарные skip/claim и редактирование анкеты, `NEEDS_INPUT`, восстановление прерванных операций и backup/restore WAL.

Живые действия на hh.ru, обращения к Gemini и отправка Telegram-сообщений не выполнялись; полные тесты используют подстановки на внешних границах.

## Известные ограничения

- Корневые фасады нужны до миграции всех потребителей и должны удаляться только после перевода импортов в следующих блоках.
- Репозитории по-прежнему возвращают словари и используют SQL напрямую; это намеренно, ORM и массовая смена моделей не вводились.
- Исторические таблицы `orders` и `subscribers` остаются в v5, хотя текущий поток LeadScout их не использует. Очистка схемы не входит в этот блок.
- Рабочая ветка изначально содержала многочисленные незакоммиченные изменения, а файлы модулей 2–3 менялись параллельно. Проверки отражают совместное состояние на момент завершения модуля 01; чужие изменения не откатывались.
- Общий frontend build, полный Ruff и полный pytest должен выполнить чат 4 после готовности остальных модулей.

## Изменения вне зоны владения до уточнения

До получения правил `parallel-v1` этот чат изменил три файла, которые теперь принадлежат другим чатам:

- `handlers.py`: путь к баннерам был переключён на `leadscout.core.paths.ASSETS_DIR`;
- `web_api.py`: путь к статической сборке был переключён на `leadscout.core.paths.WEB_DIST_DIR`;
- `tests/test_audit_regressions.py`: подмена московской даты была перенесена с совместимого `database._today` на фактический `leadscout.storage.repositories.applications.today`.

После уточнения эти файлы не редактировались и не откатывались. Чат 4 должен сохранить либо согласовать эти три изменения при интеграции.

## Что учитывать модулю 02

- Новые сервисы должны получать `Database` как зависимость и вызывать репозитории с ним явно; не импортировать `DB_PATH` и не создавать скрытые соединения.
- Если несколько запросов образуют одну бизнес-транзакцию, репозиторий должен принять текущее соединение либо предоставить одну цельную транзакционную функцию.
- Не переносить SQL обратно в сервисы и интеграции.
- До завершения постепенной миграции допустимы старые импорты из `database.py`, но новые модули должны использовать `leadscout.storage`.
- Для пропуска анкеты использовать стабильный `database.skip_pending_questionnaire`; для ожидания пользовательских данных — `database.set_operation_needs_input`.
- Нельзя менять схему v5 или формат существующих данных без отдельного согласованного блока миграции.
- Следует сохранить один процесс, текущие точки запуска и границы подстановок hh.ru, Gemini и Telegram.
