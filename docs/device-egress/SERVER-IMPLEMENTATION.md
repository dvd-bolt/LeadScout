# LeadScout Connect: серверная интеграция, настройка и проверка

Дата: 17 сентября 2026. План реализации относительно текущего репозитория. Все новые компоненты, env-переменные и endpoints ниже — предлагаемый контракт, ещё не существующие функции.

См. [обзор](README.md), [протокол](PROTOCOL.md), [платформы](PLATFORMS.md).

## 1. Основание в текущем коде

Проверены реальные точки интеграции:

| Файл | Сейчас | Необходимое изменение |
|---|---|---|
| `leadscout/integrations/browser.py` | Локальный `chromium.launch(proxy=...)`, отдельные contexts | Поддержка route lease и изолированного remote browser runner |
| `leadscout/integrations/browser_pool.py` | Ключ engine — нормализованный proxy URL; запуск engine до создания context | Типизированный ключ маршрута, лимит engine до запуска, закрытие при epoch/revision change |
| `leadscout/runtime/context.py` | Собирает factories/locks/pool/login и service graph | Инъекция DeviceService, RouteResolver, RelayClient и BrowserRunnerClient |
| `leadscout/runtime/dependencies.py` | `get_browser_engine(proxy_url)` | Account-scoped acquire route вместо самостоятельного выбора URL |
| `leadscout/integrations/login.py` | Собственная `engine_factory` вне общего pool | Общий route resolver обязателен до запроса OTP |
| `leadscout/integrations/resumes.py` | Несколько вызовов pool для профиля/резюме | Все live hh-операции получают lease |
| `leadscout/integrations/vacancies.py` | Загрузка вакансии через pool | Тот же account route, включая аудит вакансии по URL |
| `leadscout/api/routes/captcha.py` | Самостоятельно восстанавливает context | Route-aware session, закрытие на route loss |
| `leadscout/jobs/search.py` | Цикл одного аккаунта, sleep после успеха внутри context | Короткие порции + persistable ожидание устройства/next eligibility |
| `leadscout/jobs/questionnaire.py` | Подтверждённая анкета и этапы отправки | Проверка доступности и attempt uncertainty при обрыве |
| `leadscout/runtime/coordinator.py` | In-process registry и account locks | WAITING_DEVICE без занятого browser slot, route lost callbacks |
| `leadscout/runtime/scheduler.py` | Тик поиска каждые 45 минут | Upsert намерения запуска, дедупликация, запуск при готовности устройства |
| `leadscout/storage/migrations.py` | `SCHEMA_VERSION = 12` | Следующая миграция после сверки актуальной версии на реализации |
| `web/src/features/accounts/AccountSettings.tsx` | Настройки аккаунта | Выбор устройства и маршрут |
| `web/src/features/login/LoginPanel.tsx` | Вход/OTP | Маршрут выбирается до первого сетевого действия |
| `web/src/features/automation/AutomationControls.tsx` | Старт/стоп | Ожидание устройства, запуск мобильного batch |
| `docker-compose.yml`, `Dockerfile`, `Caddyfile` | app с браузером и внешним доступом | Разделение app / relay / изолированный browser runner |

`docs/refactor/ARCHITECTURE.md` описывает некоторые старые версии схемы. При реализации ориентироваться на `SCHEMA_VERSION` в коде, а не на номер в старой документации.

Текущая архитектура использует один процесс-координатор и SQLite. Несколько Uvicorn workers или самостоятельных scheduler поверх той же БД не добавляются как «масштабирование»; это потребовало бы другого механизма блокировок/leases.

## 2. Новые серверные компоненты

### 2.1 DeviceService

Привязка, owner checks, credential revision, устройства пользователя, revoke, разрешённые возможности и статус. Внедряется в AppContext, использует существующий AccessService. Администратор не получает права использовать чужое устройство для своих задач.

### 2.2 RouteResolver

Единственная точка получения сетевого пути для hh-аккаунта:

```python
async with routes.acquire(
    user_id=user_id,
    account_id=account_id,
    task_id=task_id,
    purpose="LOGIN",  # SEARCH / APPLY / QUESTIONNAIRE / RESUME / CAPTCHA / READ
) as route:
    async with browsers.acquire(route, storage_state=state) as context:
        # существующая браузерная интеграция
        ...
```

Это будущий интерфейс, не готовый вызов в репозитории. Route descriptor неизменяемый: owner, account, device, revision, session/generation, lease, deadline, внутренний proxy endpoint. Отсутствие маршрута бросает отдельный `DeviceUnavailable`, а не подставляет `None` в proxy.

### 2.3 RelayClient и relay service

Backend обращается к закрытому mTLS API relay: создать/продлить/отозвать lease, узнать подключение, принять событие loss/bytes/ready. Relay не пишет бизнес-таблицы SQLite. Единственный writer бизнес-состояния — backend; relay хранит живые sockets и tickets в памяти.

Relay public transport listener получает только `/connect/v1/control` и `/connect/v1/data`. Его internal API не публикуется Caddy. Backend auth/enrollment endpoints маршрутизируются в app. Relay авторизует control ticket через подпись и атомарный backend session activation, как описано в PROTOCOL.

### 2.4 Browser runner

В отдельном контейнере/namespace запускается Chromium. Python-код управляет им через приватный Playwright protocol. Концепция `launchServer` + `chromium.connect` поддерживается Playwright; версии клиента/сервера должны быть совместимы. [Playwright BrowserType](https://playwright.dev/python/docs/api/class-browsertype).

Репозиторий закреплён на `patchright==1.61.2`. До внедрения отдельно доказать соответствующий Node Patchright server + Python Patchright client на той же линии версий и всех используемых операциях; не считать совместимость с upstream автоматической. Если fork server не покрывает нужные функции, запасной путь — вынести текущий Python browser executor в закрытый worker с явным job RPC. Это более крупная доработка и отдельная оценка после gate, не молчаливый переход на менее совместимый CDP.

В пилоте один отдельный browser process на активный account-route lease. Не держать 100 idle browsers только потому, что 100 агентов онлайн. Ключ пула `(user_id, account_id, device_id, route_revision, session_epoch, lease_id)`. После закрытия/истечения lease process уничтожается; secret никогда не становится ключом логирования.

## 3. Приватный контракт runner

У runner будущий небольшой authenticated API, отдельный от открытого браузерного WS:

```text
POST   /internal/v1/browsers
GET    /internal/v1/browsers/{browser_id}
DELETE /internal/v1/browsers/{browser_id}
GET    /internal/v1/health
```

Создание: принимает route/lease ID, разрешённую proxy-конфигурацию и deadline. Не принимает произвольный executable, launch flags, файл профиля или внешний wsEndpoint от пользователя. Возвращает browser ID и приватный wsEndpoint с одноразовой/короткой авторизацией подключения.

Проверить реальные возможности авторизации browser WS в выбранном fork. Если произвольные headers не проверяются им самим, внутренний gateway runner обязан проверять credential до upgrade. Секретный URL сам по себе не заменяет сетевую изоляцию.

Runner использует фиксированный диапазон внутренних WS-портов, например 31000–31031, только для backend. Порты/addresses — пример развёртывания. После DELETE уничтожаются process, sockets, временный профиль и artifacts по retention. Browser endpoint не выдаётся Mini App/агенту.

Устройство является сетевым выходом, но не клиентом Playwright. Прямой CDP-доступ с телефона и передача DOM на телефон не нужны.

## 4. Изоляция сети: обязательна для режима DEVICE_REQUIRED

Одного `proxy={...}` недостаточно для гарантии отсутствия обходного пути. Ограничить browser namespace на уровне ОС:

| Источник | Разрешено |
|---|---|
| Internet → Caddy | TCP/443, TCP/80 для HTTPS redirect/ACME при необходимости |
| Caddy → app | API/статика/enrollment/auth |
| Caddy → relay | Только public control/data transport |
| app → Telegram/Gemini | Существующие серверные запросы |
| app → runner | Приватное создание/управление браузерами и browser WS |
| app → relay | Закрытый mTLS control API |
| browser namespace → relay | Только CONNECT listener на фиксированном внутреннем IP:3128 |
| browser namespace → Internet | Запрещено TCP/UDP, IPv4/IPv6 |
| agent → relay | Исходящие WSS TCP/443 |
| agent → разрешённые сайты | TCP/443 через его сетевой путь |

В новой конфигурации backend не должен оставаться местом, где случайно запускается локальный Chromium с внешним доступом. Production `DEVICE_REQUIRED` отвергает local-engine factory; в app-образе браузерный executable можно не устанавливать после миграции.

Docker `internal: true` уменьшает поверхность, но не заменяет проверку разрешённых соединений внутри сети. Browser может видеть внутренние сервисы. Нужна политика namespace OUTPUT и ingress только от backend.

Пример OUTPUT-политики для выделенного Linux browser namespace; применять только внутри специально созданного namespace, не на host вслепую:

```nft
table inet leadscout_browser {
  chain output {
    type filter hook output priority 0; policy drop;
    udp dport 53 drop
    tcp dport 53 drop
    ct state established,related accept
    oifname "lo" accept
    ip daddr 172.30.77.30 tcp dport 3128 accept
  }
}
```

Сопутствующая INPUT-политика разрешает backend IP на controller/WS ports, loopback и established replies; остальное закрыто. DNS drop стоит до loopback, чтобы Docker embedded resolver не стал обходным DNS-каналом. Proxy задаётся literal private IP. Policy по IPv6 не содержит разрешения на прямой интернет.

Firewall устанавливается init/host deployment механизмом до browser launch; Chromium не получает `CAP_NET_ADMIN`, Docker socket или privileged режим. Приложение не имеет права ослабить правила. Проверить `ct state` в чистом namespace: при создании не должно быть уже открытых внешних соединений, разрешённых established rule.

Серверный browser-runner controller использует входящие запросы и фиксированную конфигурацию, ему не требуется произвольный внешний fetch. На другом хосте использовать отдельную защищённую control сеть.

Проверки WebRTC, QUIC, IPv6, WebSocket, service worker, prefetch, redirects и DNS обязательны. Flags браузера могут уменьшить лишний трафик, но firewall — источник гарантии. `expose_network`/`exposeNetwork` при Playwright connect не включать: он способен открыть браузеру сеть управляющего клиента. [Playwright: connect options](https://playwright.dev/python/docs/api/class-browsertype).

## 5. Данные и миграции

Следующая схема — проект полей; номера версий и окончательный SQL уточняются относительно HEAD в момент реализации. Миграция не меняет владельцев существующих аккаунтов и не перепривязывает автоматически всех пользователей к новому выходу.

### 5.1 `connect_devices`

```text
id TEXT PK (UUID)
user_id INTEGER NOT NULL FK users
label TEXT NOT NULL
platform TEXT NOT NULL
public_key_jwk TEXT NOT NULL
credential_revision INTEGER NOT NULL DEFAULT 1
session_epoch INTEGER NOT NULL DEFAULT 0
status TEXT NOT NULL                   # PAIRED / REVOKED
created_at, last_seen_at, revoked_at
app_version, policy_version
UNIQUE(id, user_id)
```

Последняя живость в памяти relay отличается от persistent `status`. `last_seen_at` write throttle, например не чаще минуты и при смене состояния. Не делать SQLite write на каждый packet/heartbeat всех устройств.

### 5.2 `account_routes`

```text
account_id INTEGER NOT NULL
user_id INTEGER NOT NULL
mode TEXT NOT NULL                     # LEGACY / DEVICE_REQUIRED
device_id TEXT NULL
revision INTEGER NOT NULL DEFAULT 1
updated_at
PRIMARY KEY(account_id, user_id)
FOREIGN KEY(account_id, user_id) → hh_accounts(id, user_id)
FOREIGN KEY(device_id, user_id) → connect_devices(id, user_id)
```

Для SQLite composite FK нужен UNIQUE index `(id,user_id)` у `hh_accounts`. Смена route проверяет owner обеих сторон и `expected_revision`. `DEVICE_REQUIRED` допускает unassigned device как «нужно подключить», но запрещает сетевую работу. Legacy proxy поддерживается только для ещё не мигрировавших записей; это не fallback нового режима.

### 5.3 Дополнительные таблицы

| Таблица | Основные поля/назначение |
|---|---|
| `connect_enrollments` | public key, code hash, poll secret hash, owner approval, expiry, attempts; краткоживущие |
| `connect_auth_challenges` | nonce hash/ID, device, expiry, used; TTL cleanup |
| `connect_session_grants` | session ID, device, epoch, relay instance, grant expiry, revoked |
| `connect_batches` | user/account/device, immutable limits, intent status, deadline, started/finished, revision |
| `route_leases` | audit IDs, task/account/device, epoch, expiry/state; без proxy/data secrets |
| `automation_intents` | user/account, kind, not_before, waiting_reason, unique active intent |
| `connect_usage` | агрегаты дня/устройства: bytes up/down, active seconds, ошибки |
| `connect_events` | owner-scoped события pairing/revoke/route loss, безопасные причины |

`batch_intent` одноразовый, разрешает запуск только заданному устройству владельца и истекает, например за 5 минут. Для фоновых desktop-заданий постоянная пользовательская настройка автоматизации служит разрешением, но конкретные leases остаются короткими.

К `application_attempts`, resume attempts и trace metadata добавить nullable `device_id`, `route_lease_id`, `route_revision`, `session_epoch`, `network_generation`. Они нужны для объяснения обрыва и не подменяют бизнес-состояние.

Нельзя сохранять mutable route secret прямо в строке аккаунта: credentials меняются при reconnect и могут попасть в экспорты. Все живые secrets только в памяти и очищаются при закрытии. После рестарта relay все leases недействительны; backend закрывает зависимые контексты, а не пытается продолжить старый TCP.

## 6. API Mini App и агента

### 6.1 Пользовательский API, действующая сессия + CSRF на mutations

| Метод | Path | Результат |
|---|---|---|
| GET | `/api/v1/devices` | Только устройства текущего владельца и safe status |
| POST | `/api/v1/devices/pair/preview` | Проверка введённого кода, fingerprint, имя |
| POST | `/api/v1/devices/pair/approve` | Owner approval для enrollment |
| PATCH | `/api/v1/devices/{id}` | Имя и пользовательские ограничения с revision |
| POST | `/api/v1/devices/{id}/revoke` | Отзыв всех routes и device key revision |
| GET | `/api/v1/accounts/{id}/route` | Устройство, mode, revision, причина ожидания |
| PUT | `/api/v1/accounts/{id}/route` | `{device_id, mode, expected_revision}` |
| POST | `/api/v1/accounts/{id}/connect-batches` | Конечный batch intent, launch URL |
| GET | `/api/v1/connect-batches/{id}` | Прогресс/состояние/терминальный результат |
| POST | `/api/v1/connect-batches/{id}/cancel` | Остановка дальнейшего выполнения |
| POST | `/api/v1/devices/{id}/probe` | Запланировать проверку только собственного устройства |

Idempotency key на pair approve, batch create/cancel и route change, где нужно. Несовпадение revision — 409. Чужой объект — согласованный с текущим API 404/403 без утечки статуса. Ответы никогда не содержат private key, hh storage state, internal wsEndpoint или proxy secret.

### 6.2 API агента

Enrollment/auth paths определены в PROTOCOL. Дополнительно используются `/connect/v1/batches/{id}`, `/connect/v1/policy` и `/connect/v1/device/revoke-self` с отдельным scoped agent API token. Scope агента не даёт чтения резюме или управления настройками другого пользователя. Возвращаемое описание batch — safe metadata/limits/progress без текста резюме и письма.

### 6.3 Internal API backend↔relay

```text
POST /internal/v1/sessions/activate      # backend authority расходует ticket jti
POST /internal/v1/sessions/renew        # backend выдаёт новый 90s grant
POST /internal/v1/leases                # backend→relay; waits device acceptance
POST /internal/v1/leases/{id}/renew
POST /internal/v1/leases/{id}/revoke
POST /internal/v1/devices/{id}/revoke
GET  /internal/v1/devices/{id}/status
POST /internal/v1/relay-events          # relay→backend, session scoped
```

Это список логических операций; activate/renew session и relay-events обслуживает backend, операции lease/device status — relay. Документировать в отдельных OpenAPI contracts, чтобы не перепутать владельца endpoint. mTLS peer role проверяется на каждом listener; публичный Caddy не маршрутизирует `/internal/`.

Relay events содержат `event_id`, `relay_instance_id`, epoch и sequence; backend дедуплицирует и отвергает устаревшие epochs. Heartbeat не повышает access role. При потере канала управления session grant ограничивает lifetime.

## 7. Scheduler и очередь

Нынешний tick `trigger_all_users_search` каждые 45 минут остаётся источником намерения выполнить работу. Новый порядок:

1. Проверить access/account settings/daily limit.
2. Upsert один активный `automation_intent` на account+kind. Повторный tick обновляет желание, но не создаёт ещё одну копию задания.
3. Проверить назначенное устройство и допустимый execution mode. Если недоступно — WAITING_DEVICE без browser slot и без удержания account lock.
4. Fair queue выбирает готовые accounts round-robin; учитывать user/day quotas и `not_before`.
5. Короткий account lock + повторная проверка access/route revision. Получить capacity reservation, затем lease. При недоступности немедленно освободить reservation, не ждать минуты внутри глобального lock.
6. Повторно проверить route/access после ожидания browser capacity. Создать engine только после получения лимита active processes.
7. Выполнить ограниченную порцию: одна обработанная вакансия или один ограниченный шаг чтения; не разрывать submit/confirm.
8. Сохранить session/cursor/attempt outcome, закрыть context/engine/lease, поставить следующий `not_before`.
9. User stop отличается от network pause: stop выключает автоматизацию и отменяет intent; pause сохраняет намерение и квоты.

Событие READY может инициировать drain готовой очереди, но не запускать автоматически неподтверждённую анкету или новый мобильный batch. `SUBMITTING` никогда не трактуется как безопасная ожидающая задача.

Для interactive login/CAPTCHA предусмотреть отдельные ограниченные slots и deadline, чтобы 10-минутный OTP не остановил все аккаунты. Суммарный cap памяти/браузеров обязателен независимо от отдельной interactive квоты.

Сейчас slot считается по BrowserContext, а `get_engine()` может запустить engine раньше. При отдельном engine на маршрут это особенно опасно: 100 ожиданий создадут 100 Chromium ещё до semaphore. Добавить лимит процесса до launch, а не просто поднять `MAX_CONCURRENT_BROWSERS`.

## 8. Что маршрутизировать

| Операция | Требуется устройство? | Поведение офлайн |
|---|---|---|
| Открытие Mini App / история / настройки | Нет | Работает через backend |
| ИИ-аудит уже сохранённого резюме | Нет | Работает |
| Анализ вакансии по введённому тексту | Нет | Работает |
| Анализ вакансии по ссылке с загрузкой hh.ru | Да | Ожидание или предложить вставить текст |
| Первичный/повторный login и OTP | Да | Не обращаться к hh.ru до READY |
| Капча, её обновление и ввод | Да | Сохранить challenge state; восстановить по новому маршруту с проверкой |
| Поиск/загрузка вакансий | Да | Очередь |
| Обычный отклик/письмо | Да | Очередь; после submit uncertainty reconciliation |
| Черновик анкеты | Нет для локального редактирования | Работает |
| Проверка схемы и отправка анкеты | Да | Новая проверка revision + подтверждённое намерение |
| Локальный импорт PDF/ИИ-разбор | Нет | Работает |
| Синхронизация, публикация, изменение резюме hh.ru | Да | Ожидание; спорные изменения не повторять автоматически |

Сетевые пути должны быть инвентаризированы grep-проверкой и тестами. Не должно остаться `engine_factory(proxy_url=None)` у login, CAPTCHA, resumes или одноразового URL audit в DEVICE_REQUIRED. Gemini и Telegram API не отправляются через устройство: это увеличило бы трафик и не относится к назначению.

## 9. Развёртывание пилота

### 9.1 Домены

```text
app.<product-domain>           нынешняя Mini App и API
connect.<product-domain>       pairing/auth + control/data WSS + launch links
egress-check.<product-domain>  узкий HTTPS probe
downloads.<product-domain>     signed installers/metadata
```

Это примеры, не приобретённые домены. Все могут использовать один VPS/публичный IP. IP relay не станет адресом исходящего соединения к hh.ru, если выполняется device route и browser isolation.

### 9.2 Сервисы

```text
caddy          public HTTPS, verified routes, websocket upgrade
app            текущий backend + DeviceService/RouteResolver
relay          public WSS transport + private CONNECT/control
browser-runner no Internet egress, private controller/browser WS
probe          только nonce echo и observed peer address
backup         существующий SQLite backup, новые owner/device records включены
```

Не запускать второй backend scheduler в browser-runner. Browser-runner не получает доступ к основной SQLite, Telegram bot token или Gemini API key. Relay получает ключи проверки билетов, а не SESSION_ENCRYPTION_KEY для hh-сессий.

### 9.3 Предлагаемые env-переменные

```dotenv
# Будущие настройки; пока отсутствуют в конфигурации приложения.
CONNECT_ENABLED=true
CONNECT_PUBLIC_ORIGIN=https://connect.example.com
CONNECT_RELAY_INTERNAL_URL=https://relay.internal:8443
CONNECT_RUNNER_INTERNAL_URL=https://runner.internal:8444
CONNECT_PROXY_SERVER=http://172.30.77.30:3128
CONNECT_ROUTE_DEFAULT=DEVICE_REQUIRED
CONNECT_HEARTBEAT_SECONDS=25
CONNECT_OFFLINE_SECONDS=75
CONNECT_SESSION_GRANT_SECONDS=90
CONNECT_LEASE_TTL_SECONDS=120
CONNECT_LEASE_RENEW_SECONDS=30
CONNECT_MAX_STREAMS_PER_DEVICE=16
CONNECT_MAX_ACTIVE_TASKS_PER_DEVICE=1
CONNECT_CONTROL_MAX_BYTES=16384
CONNECT_DATA_CHUNK_BYTES=65536
CONNECT_QUEUE_CHUNKS_PER_DIRECTION=2
CONNECT_PROBE_ORIGIN=https://egress-check.example.com
CONNECT_ALLOWED_POLICY_FILE=/run/config/connect-policy.json
CONNECT_ALLOW_SERVER_FALLBACK=false
```

mTLS key paths, ticket-signing key, verification keyset и policy signer задаются через отдельные secrets mounts. Существующий `SESSION_ENCRYPTION_KEY` не заменяется. Порты и subnet проверяются на конфликт с сетью хоста. Server cap active browsers выбирается после load test, не выводится автоматически из 100 зарегистрированных устройств.

### 9.4 Caddy: логическая конфигурация

Пример после реализации соответствующих endpoints; не вставлять вместо текущего Caddyfile до сборки сервисов:

```caddyfile
connect.example.com {
    @transport path /connect/v1/control /connect/v1/data
    handle @transport {
        reverse_proxy relay:8081 {
            stream_close_delay 5m
        }
    }
    @agent_api path /connect/v1/enrollments* /connect/v1/auth/* /connect/v1/batches/* /connect/v1/policy /connect/v1/device/revoke-self
    handle @agent_api {
        reverse_proxy app:8000
    }
    @launch path /connect/* /session/* /.well-known/*
    handle @launch {
        reverse_proxy app:8000
    }
    handle {
        respond "Not found" 404
    }
}
```

Caddy поддерживает WebSocket proxy. Reload по умолчанию может закрывать существующие WebSocket; delay уменьшает резкий массовый reconnect, но не даёт нулевого простоя. [Caddy reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

Не логировать Authorization и pairing codes. Для transport access logs достаточно path без query/headers, connection result, duration и internal trace ID. TLS termination у Caddy защищает путь device↔edge, внутренний hop живёт только в выделенной сети; если инфраструктура разнесена, hop шифруется отдельно.

### 9.5 Порядок запуска

1. Создать изолированный staging, отдельную БД/секреты и контролируемый destination server.
2. Настроить DNS/TLS и закрытые networks.
3. Собрать pinned relay/core/runner и подписанный desktop-agent.
4. Применить browser namespace firewall до запуска Chromium; проверить правило из самого namespace.
5. Backup текущей SQLite штатным backup API; проверить восстановление в отдельную БД.
6. Применить additive migration, запустить app в single-coordinator режиме.
7. Запустить relay/runner; readiness проверяет control authentication и deny-direct network.
8. Подключить собственное тестовое устройство через реальный Mini App flow.
9. Прогнать весь acceptance на контролируемом сервере без чужих аккаунтов.
10. С подтверждённым намерением владельца выполнить ограниченный end-to-end hh-сценарий, включая реальный отклик только на выбранную им вакансию.
11. Включать DEVICE_REQUIRED на выбранных pilot accounts; не переключать все аккаунты автоматически.
12. Собрать метрики, затем расширять rollout на платформы с пройденным lifecycle gate.

## 10. Наблюдаемость и эксплуатация

Метрики с ограниченной cardinality:

```text
connect_devices_online{platform,execution_mode}
connect_streams_active
connect_open_duration_seconds{result}
connect_bytes_total{direction}
connect_reconnect_total{platform,reason}
connect_waiting_jobs{reason}
connect_route_loss_total{stage}
connect_uncertain_attempts_total{operation}
connect_browser_direct_egress_block_total
connect_batch_completion_ratio{platform,os_major}
connect_browser_slot_wait_seconds
connect_memory_bytes / process_fds / queue_bytes
```

Не использовать user/account/stream UUID как labels Prometheus. Они доступны только в owner-scoped audit по request ID; секреты и содержимое трафика отсутствуют. Агрегаты RX/TX нужны для экономических замеров; это не packet capture.

События/пороговые сигналы:

- Рост tunnel failures по версии агента → остановить rollout этой версии.
- Любое доказанное direct browser egress → блокировать DEVICE_REQUIRED запуск до исправления.
- Рост неопределённых отправок → приостановить новые изменения для затронутых маршрутов, провести reconciliation.
- Нехватка памяти/FD → отказ новых leases с понятным SERVER_BUSY, не убийство произвольных пользовательских процессов.
- Device offline → одно уведомление при ожидающей работе, затем тихое ожидание; повтор по значимой смене статуса.

Начальная retention policy для оценки: технические события 14 дней, дневные usage aggregates 90 дней, pairing/challenges удаляются после короткого TTL, полные диагностические IP не дольше 24 часов. Согласовать с существующей политикой хранения продукта; это проектные значения. Нельзя удалять нерешённые внешние attempts только по короткой диагностической retention.

## 11. Рестарт, отзыв, rollback

### Relay restart

Все in-memory tickets/leases/streams исчезают. Новые соединения проходят свежую авторизацию. Backend по событию/timeout закрывает route и browser, восстанавливает безопасную очередь и спорные attempts разными путями. `WAITING_DEVICE` не должен становиться «успешно выполнено».

### Backend restart

Backend отзывает старые session grants при recovery либо даёт им истечь в пределах 90 секунд; до восстановления бизнес-авторитета новые leases не выдаются. `attempts` в SUBMITTING/CONFIRMING восстанавливаются как неопределённые. Никаких повторных отправок в startup hook.

### Устройство отозвано / пользователь заблокирован

В транзакции фиксируется запрет и повышается credential/route revision. Затем relay прекращает streams, browser contexts закрываются, задания получают точную причину. Если relay недоступен, его grants ограничивают старый доступ; UI не сообщает «всё закрыто мгновенно», пока нет подтверждения/истечения. Локальная Stop-кнопка агента закрывает socket немедленно независимо от backend.

### Rollback

Feature flag останавливает новые Connect-задачи. Drain/close все leases. Additive schema не удалять автоматически. Старый бинарник, который игнорирует DEVICE_REQUIRED, нельзя запускать поверх таких аккаунтов без защиты: иначе он вернётся к direct proxy=None. Rollback compatibility должен либо понимать route mode и блокировать эти задания, либо полностью выключать их automation до возврата версии. Никакого автоматического server fallback.

## 12. Проверки протокола и сети

| ID | Проверка | Ожидаемый результат |
|---|---|---|
| P01 | Одна пара browser↔device↔controlled HTTPS | Destination видит выход устройства; TLS проверяется Chromium |
| P02 | Два владельца, перекрёстные device/account/lease IDs | Отказ без выдачи статуса и трафика чужого владельца |
| P03 | Отозванный/истёкший control/data/proxy credential | Отказ; живые revoked streams закрываются |
| P04 | Повтор data ticket / старая epoch | Один принимается не более раза, старое поколение не работает |
| P05 | Huge/fragmented WS message, malformed JSON, неизвестный opcode | Ограниченный расход памяти, protocol close |
| P06 | Медленный uplink/downlink, zero window | Bounded buffers, backpressure, остальные устройства работают |
| P07 | FIN в одном направлении, RST, open timeout | Корректный half-close/cleanup, без FD leak |
| P08 | Localhost, metadata, IPv4-mapped IPv6, DNS rebinding | Не открывается socket к запрещённому адресу |
| P09 | IPv6-only + NAT64 | Контролируемый HTTPS работает через устройство или честный unsupported status |
| P10 | Wi-Fi→cellular / смена VPN | Закрытие старых sockets, новая generation, перепроверка |
| P11 | Relay crash после bytes submit | UNCERTAIN, без автоматического повторного POST |
| P12 | Stop/revoke при HTTP/2 keep-alive | Закрывается существующий CONNECT, не только новые |
| P13 | Browser proxy выключен, QUIC/WebRTC/DNS/IPv6 попытки | Нет прямого выхода из namespace |
| P14 | Подмена заголовка X-Forwarded-For на probe | Не меняет доверенный наблюдаемый адрес |
| P15 | Неверный сертификат/host/policy signature | Fail closed, без ignore TLS errors |

Контролируемый destination должен уметь специально задержать ответ после записи операции: это позволяет проверить ambiguous outcome без реальных повторных откликов на hh.ru.

## 13. Регрессионные проверки продукта

1. Вход и OTP через device route; повтор OTP только по пользовательскому действию.
2. Повторный вход не пересекается с браузерной работой того же аккаунта.
3. Загрузка вакансий, скоринг, письмо, подтверждённый обычный отклик.
4. Анкета: чтение → черновик → revision conflict → одобрение → отправка → проверка результата.
5. Капча в login, search, questionnaire, resume publish; правильный route после восстановленного контекста.
6. Резюме: sync, импорт PDF, редактирование черновика, preflight, publish, ambiguity reconciliation.
7. У двух аккаунтов раздельные storage states; один device не открывает обе browser jobs одновременно при cap=1.
8. У владельцев не пересекаются маршруты/статистика/уведомления/административные ограничения.
9. Device offline не занимает slots, не выключает выбранную автоматизацию и не размножает intents.
10. Stop account / Stop all / admin revoke / shutdown закрывают resources в существующей системе task scope.
11. Паузы после отклика сохраняются, но engine/context capacity освобождается в безопасной точке.
12. Backup+restore schema с устройствами не восстанавливает живые tickets/streams.
13. Server rollback не возобновляет direct hh-трафик.

Опора на существующие тесты: `test_context_isolation.py`, `test_tasks_and_browser.py`, `test_audit_regressions.py`, `test_questionnaire_e2e.py`, `test_questionnaire_revisions.py`, `test_application_flow.py`, `test_web_api.py` и соответствующие resume tests. Точные selectors/imports сверять с текущим HEAD.

Для документов эти тесты не запускаются как доказательство будущей реализации. При разработке добавить meaningful tests маршрутизации и отказов, затем выполнить существующий набор, затронутый DI/browser/scheduler изменениями.

## 14. Нагрузка для 100 пользователей

Проверять отдельно: 100 зарегистрированных устройств, 100 online control sockets и число реально одновременно работающих браузеров. Это разные нагрузки.

Сценарий staging:

1. 100 симулированных агентов с real WSS и controlled destinations.
2. 100 idle control-соединений минимум 2 часа; проверить heartbeat и memory plateau.
3. 10 активных browser tasks по 16 streams максимум: до 160 data sockets и 100 control sockets плюс внутренние соединения; cap browser tasks увеличивать только по измеренной RAM/CPU.
4. Ограничить uplink отдельных устройств, отключить 20%, массово сменить epoch, reload Caddy/relay restart.
5. Проверить round-robin fairness, отсутствие starvation у login/OTP, ограниченное время в очереди.
6. Увеличивать длительность soak до суток на desktop-режиме; это тест инфраструктуры, не обещание суточного мобильного сеанса.

При 160 streams app-очереди по 256 KiB дают около 40 MiB; дополнительно нужны TLS/socket/kernel buffers, runtime и браузеры. Нельзя использовать это число как оценку всей RAM VPS. Малые TLS streams создают handshake/CPU overhead; после измерений можно оценить multiplexing v2, но он не нужен для первого прототипа.

Серверные данные проходят relay; TCP-over-WebSocket увеличивает overhead и задержки. На плохом мобильном соединении bytes идут hh.ru↔phone↔VPS; это может быть медленнее прямого серверного доступа. Настройки 30-секундных navigation timeouts проверяются на мобильной сети, не повышаются бесконечно.

## 15. План реализации с результатами каждого этапа

### G0. Проверка платформ и remote browser compatibility

- Минимальный core с control+одним stream.
- Windows/macOS build на controlled HTTPS.
- Реальный iPhone: finite BGContinuedProcessingTask, возврат в Telegram, lock/cancel.
- Реальный Android: выбранный FGS type, экран off, quota/stop.
- Node/Python Patchright remote protocol на pinned versions: storage state, routes, file upload, download, screenshot, cancellation.

Результат: фактическая таблица возможностей. Если iOS background не проходит, явно сужается заявленный режим до дальнейшего вложения в полную оболочку.

### G1. Relay/core и identity

- Enrollment, двойное подтверждение, подписи challenges, control/data tickets.
- CONNECT proxy, allowlist, agent DNS, queues, FIN/RST.
- Epoch/lease/revoke, protocol test vectors и negative tests.
- No hh account/session data на агенте.

Результат: независимый сетевой компонент с тестами P01–P15 на controlled endpoints.

### G2. Серверная сеть и RouteResolver

- Additive DB migration, DeviceService/API.
- Browser runner namespace + firewall.
- Route-aware browser factory для login/search/resume/captcha/questionnaire/audit.
- Отличимые errors DEVICE_OFFLINE, ROUTE_LOST, ROUTE_POLICY_DENIED, SERVER_BUSY.

Результат: невозможность обращения hh.ru без выбранного device route для пилотного аккаунта.

### G3. Восстановление и scheduler

- Durable intents, fair scheduling и освобождение contexts между безопасными порциями.
- Сохранение attempt stages, reconciliation всех внешних mutations.
- Batch plans/limits/progress и OS lifecycle integration.
- Admin stop/revoke/shutdown и rollout/rollback fences.

Результат: отсутствие двойных отправок и безнадзорных маршрутов после сбоев.

### G4. Клиенты и Mini App

- Device panel, pairing, route selection, waiting status, launch links.
- Signed Windows/macOS packages и корректный автозапуск.
- Android APK pilot/заявленный FGS режим.
- iOS TestFlight и только доказанный execution mode.
- Diagnostics, usage counters, local quotas, update/uninstall.

Результат: весь путь пользователя проходит без терминала и редактирования proxy URL.

### G5. Пилот и выпуск

- Полные product regressions, load/soak, аппаратная матрица.
- Проверка установки обычным пользователем на чистом устройстве.
- Измеренная стоимость на пользователя и support burden.
- Доступный массовый канал распространения для каждой продаваемой платформы.
- Документация ограничений сна/фона в продукте и тарифах.

Нет достоверной оценки недель/стоимости до G0: iOS feasibility, магазинное распространение и remote browser compatibility — существенные неопределённости. Сначала измерить и снять их, затем оценивать по конкретным задачам.

## 16. Открытые вопросы, для которых заранее задан способ проверки

| Вопрос | Как закрывается | До ответа |
|---|---|---|
| iOS конечный сетевой сеанс стабилен? | Аппаратный G0 + review выбранного использования API | Background считается экспериментальным |
| Подходит ли Android FGS type? | Сопоставление назначения + pilot + проверка распространения | Только bounded sessions |
| Какие hh/CDN hosts необходимы? | Controlled hh flow и метаданные запросов без cookies/content | Узкая allowlist, неизвестное блокируется |
| Patchright remote совместим? | Полный browser integration smoke на pinned versions | Runner topology — проект, не подтверждённый факт |
| Сколько browser tasks потянет VPS? | Load test RAM/CPU/FD/latency | Небольшой cap, очередь |
| Сколько трафика/батареи у пользователя? | RX/TX и замеры физического устройства | Только примерная модель, без рекламных цифр |
| hh.ru допускает коммерческий сценарий? | Условия/согласование с платформой | Нет гарантии отсутствия ограничений по IP/аккаунту |

## 17. Definition of Done

Релиз готов для конкретной платформы, когда все её заявленные пользовательские сценарии проходят на реальном устройстве, продукт не выходит через сервер при потере устройства, владельцы изолированы, спорные изменения не повторяются, имеется подписанный доступный установщик и проверенный путь обновления/отзыва.

Windows/macOS могут выйти раньше мобильных платформ. Наличие общей protocol implementation не означает одинаковых гарантий фоновой работы на всех ОС.
