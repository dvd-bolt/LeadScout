# LeadScout Connect: протокол v1 и границы доверия

Дата: 17 сентября 2026. Это спецификация будущего протокола; перечисленные endpoints, события и параметры ещё не реализованы.

Связанные документы: [обзор](README.md), [платформы](PLATFORMS.md), [сервер](SERVER-IMPLEMENTATION.md).

## 1. Участники и идентификаторы

| Поле | Значение |
|---|---|
| `user_id` | Владелец LeadScout; вычисляется сервером из действующей авторизации |
| `device_id` | Случайный UUID устройства, выдаётся сервером после привязки |
| `account_id` | Аккаунт hh.ru, принадлежность проверяется при выдаче lease |
| `device_session_id` | Случайный ID текущего control-соединения |
| `session_epoch` | Серверное поколение сессии; растёт после переподключения |
| `network_generation` | Счётчик агента при смене сетевого пути |
| `route_revision` | Версия привязки аккаунта к устройству |
| `batch_id` | Конечный мобильный сеанс / разрешённая партия работы |
| `lease_id` | Случайный UUID маршрута задания |
| `stream_id` | Случайный UUID конкретного TCP-соединения |
| `attempt_id` | Существующий идентификатор внешней бизнес-операции |

IP никогда не служит идентификатором пользователя или способом авторизации. Один человек может иметь несколько устройств, несколько устройств — один NAT-адрес. Переданные агентом platform/version — диагностические сведения, не криптографическое доказательство платформы.

## 2. Привязка устройства

За основу UX берётся модель подтверждения устройства с другого авторизованного интерфейса. Это собственный протокол привязки, не заявляемый как реализация OAuth Device Grant. Идеи одноразового device code, короткого user code и ограниченного polling описаны в [RFC 8628](https://www.rfc-editor.org/rfc/rfc8628).

### 2.1 Регистрация кандидата

Агент генерирует ECDSA P-256 ключ. Закрытый ключ хранит ОС; core получает callback `sign(bytes)`. Агент отправляет:

```http
POST /connect/v1/enrollments
Content-Type: application/json

{
  "public_key_jwk": {"kty":"EC","crv":"P-256","x":"...","y":"..."},
  "platform": "ios",
  "app_version": "0.1.0",
  "device_label": "Мой iPhone",
  "client_nonce": "random-base64url"
}
```

Сервер валидирует ключ/размеры, возвращает `enrollment_id`, 256-битный `poll_secret`, короткий `user_code` из 10 символов Crockford Base32, `expires_in=600`, `poll_interval=5`. Хранит хэш секрета и хэш кода. Кандидат ещё не владелец устройства и не может подключаться к relay. Polling: `GET /connect/v1/enrollments/{id}` с poll secret в Authorization; повторять не чаще выданного interval, соблюдать 429/Retry-After. `DELETE` этого же endpoint отменяет незавершённую привязку.

Код вводится в авторизованной Mini App. Ссылка может содержать только этот одноразовый код/ID, не закрытый ключ или credential. Полный секрет polling не передавать в deep link/QR. Rate limit создания: начально 5/минуту на IP с мягкой обработкой общего NAT, глобальный лимит кандидатов и риск-ориентированное ограничение. Подбор кодов: 5 неверных попыток на пользователя за 10 минут, ограничение на код и глобальную частоту; успешное угадывание всё равно не отменяет подтверждение в агенте.

### 2.2 Два подтверждения

1. Mini App вызывает `POST /api/v1/devices/pair/preview` с кодом, действующей сессией и CSRF. Получает платформу, имя и короткий отпечаток ключа.
2. Пользователь сверяет отпечаток с агентом и подтверждает `POST /api/v1/devices/pair/approve`. Сервер атомарно назначает кандидату владельца; повтор другого пользователя запрещён.
3. Агент получает через polling состояние `OWNER_APPROVAL_PENDING_DEVICE`, отображаемое имя владельца и случайный 32-байтный `confirm_nonce` со сроком не более оставшегося TTL enrollment. Повторный polling возвращает тот же nonce, а не меняет его при каждом запросе.
4. Пользователь нажимает «Подключить». Агент подписывает серверный nonce и отправляет `POST /connect/v1/enrollments/{id}/confirm` с `poll_secret` в Authorization header.
5. Только теперь сервер создаёт `device_id`, сохраняет публичный ключ и `credential_revision=1`, помечает код использованным. Ответ содержит `device_id` и revision. До исходного срока истечения enrollment хранится терминальный результат и хэш poll secret: повтор того же подписанного confirm после потери ответа возвращает тот же результат, не создавая второго устройства. Затем данные восстановления удаляются. Poll secret никогда не авторизует обычные API или подключение к relay.

Подпись связывает `enrollment_id`, nonce и хэш публичного ключа. Отмена, истечение TTL или повторное использование возвращают терминальное состояние; агент начинает новую привязку. Имя устройства — недоверенный текст, нормализуется и экранируется.

Двойное подтверждение уменьшает риск подмены deep link и привязки компьютера к чужому Telegram. Установщик сам по себе не считается авторизацией.

## 3. Авторизация агента и токены

### 3.1 Получение connection ticket

1. `POST /connect/v1/auth/challenge` с `device_id`.
2. Сервер выдаёт `challenge_id`, 32 случайных байта nonce, TTL 60 секунд, фиксированную строку audience.
3. Агент подписывает точные UTF-8 bytes:

```text
LSC-AUTH-V1\n{device_id}\n{challenge_id}\n{nonce_base64url}\n{audience}\n{credential_revision}
```

Здесь `\n` обозначает один LF; в конце строки дополнительного LF нет. UUID/nonce/audience не допускают CR/LF. Подпись wire-format — 64 байта `r||s`, base64url без padding; платформенные DER-подписи преобразует библиотека JOSE/криптографии, не самописный ASN.1-парсер.

4. `POST /connect/v1/auth/complete` с подписью. Проверки: владелец активен, устройство не отозвано, ключ/ревизия совпадают, challenge не использован, TTL действителен.
5. Сервер возвращает подписанный короткий control ticket (JWT, разрешён только ES256) с `aud=connect-control`, `sub=device_id`, `user_id`, `credential_revision`, `jti`, `exp=now+60s`. Отдельно выдаёт 10-минутный agent API token с `aud=connect-api`, привязанный к device/credential revision и ограниченным scopes `own-batch:read`, `own-batch:status`, `own-policy:read`, `self:revoke`.

Ticket одноразовый, расходуется атомарно при WS handshake. Relay проверяет подпись и online-активацию через закрытый control API backend; недоступность API означает отказ, а не допуск по устаревшему кэшу. Подпись проверяется стандартной библиотекой с фиксированными `iss/aud/alg`, `kid` только из известного keyset.

Agent API token применяется только к HTTPS REST запросам агента. Backend на каждом вызове проверяет актуальную credential revision/owner access; отзыв действует без ожидания 10 минут. Новый token получается новой challenge-подписью; долговечного refresh secret нет. Получение нового ticket без его использования не прерывает текущий control WS и не повышает epoch. Control ticket не даёт доступа к Mini App API, а agent API token не принимается как control/data ticket.

### 3.2 Control WebSocket

```http
GET /connect/v1/control
Authorization: Bearer <control-ticket>
Upgrade: websocket
Sec-WebSocket-Protocol: leadscout-control.v1
```

Нативный клиент умеет Authorization header. Mini App не подключается к этому endpoint. Токены не передаются в query string. TLS 1.2+; предпочтительно TLS 1.3; стандартная проверка имени и trust store без `insecure`-режима в release.

Relay не требует, чтобы ticket оставался неистёкшим весь срок WS: после handshake действует server-side session grant, авторитет которого обновляется каждые 30 секунд через backend. Срок такого grant 90 секунд. Если renew не удался, новые streams запрещаются, действующие закрываются не позже окончания grant. Это верхняя граница работы при разрыве управляющего канала backend↔relay; прямой отзыв в норме применяется немедленно.

### 3.3 Data ticket

Каждый новый stream получает отдельный 256-битный одноразовый opaque ticket TTL 15 секунд, хранимый relay в памяти по хэшу. Он связан с `device_id`, `device_session_id`, `session_epoch`, `lease_id`, `stream_id`, `network_generation`, hostname и port. Передаётся устройству только внутри аутентифицированного control WS, обратно — в Authorization header нового data WS.

Срок относится к открытию data WS. После принятия доступ ограничен сроком session grant и lease. Повторный handshake, другой агент или другая epoch отклоняются.

## 4. Control-сообщения

UTF-8 JSON, максимум 16 KiB. Общая оболочка:

```json
{
  "v": 1,
  "type": "HELLO",
  "message_id": "uuid",
  "session_epoch": 17,
  "payload": {}
}
```

`session_epoch` отсутствует только у первого HELLO до WELCOME. Неизвестный `type`, несовместимая версия, дублирующийся JSON key, лишние критические поля или превышение размера дают protocol error. Используется явная schema validation; неизвестные необязательные поля разрешаются только если описаны как extension.

| Тип | Направление | Назначение |
|---|---|---|
| `HELLO` | Agent→Relay | Версия, возможности, ограничения сети, текущий batch |
| `WELCOME` | Relay→Agent | Session ID, epoch, heartbeat, действующая policy version |
| `PING`, `PONG` | Оба | Liveness и монотонный RTT; не доказательство доступности hh.ru |
| `STATUS` | Agent→Relay | Режим ОС, quota remaining, network generation, причита остановки |
| `LEASE_OFFER` | Relay→Agent | Владелец/псевдоним аккаунта, срок, batch, квоты маршрута |
| `LEASE_ACCEPT`, `LEASE_REJECT` | Agent→Relay | Локальное подтверждение policy и доступности |
| `OPEN` | Relay→Agent | Разрешённые host/port, stream ID, data ticket |
| `OPEN_ERROR` | Agent→Relay | Нормализованная ошибка DNS/connect/policy |
| `CANCEL_STREAM` | Relay→Agent | Отмена ожидающего или текущего stream |
| `DRAIN` | Оба | Не начинать новую работу, завершить безопасные участки до deadline |
| `REVOKE` | Relay→Agent | Закрыть lease/устройство немедленно |
| `BATCH_PROGRESS` | Relay→Agent | Подтверждённые шаги, итоговые состояния, короткая подпись |
| `BATCH_DONE` | Relay→Agent | Конечный результат конечной задачи |
| `NETWORK_CHANGED` | Agent→Relay | Закрытие старых streams, новое поколение сети |

Пример HELLO payload:

```json
{
  "platform": "android",
  "app_version": "0.1.0",
  "capabilities": ["tcp-connect", "session-background"],
  "execution_mode": "USER_SESSION",
  "batch_id": "uuid",
  "network_generation": 3,
  "metered": true,
  "cellular_allowed": true,
  "max_streams": 16
}
```

Сервер не доверяет заявленному `max_streams`: эффективный лимит — минимум серверной политики, пользовательского лимита и возможностей агента. `capabilities` не гарантируют, что ОС не остановит процесс.

## 5. Lease и CONNECT от браузера

Backend создаёт lease через закрытый mTLS control API relay после проверки владельца и режима ОС. Relay предлагает его устройству; отказ/таймаут не запускает браузер. Поля:

```json
{
  "lease_id": "uuid",
  "user_id": 123,
  "account_id": 456,
  "device_id": "uuid",
  "device_session_id": "uuid",
  "session_epoch": 17,
  "network_generation": 3,
  "route_revision": 2,
  "task_id": "uuid",
  "batch_id": "uuid-or-null",
  "policy_version": 1,
  "max_streams": 16,
  "expires_at": "RFC3339 UTC"
}
```

Начальный TTL lease 120 секунд, renew раз в 30 секунд. Максимальный срок не превышает batch/пользовательское разрешение. Lease нельзя продлевать после локальной остановки, смены сети, отзыва, завершения batch или блокировки пользователя.

Chromium получает внутреннюю proxy-конфигурацию:

```python
proxy = {
    "server": "http://172.30.77.30:3128",  # пример внутреннего адреса relay
    "username": lease_id,
    "password": one_time_generated_proxy_secret,
}
```

Proxy secret случайный, действует на время lease, не сохраняется в `hh_accounts.proxy_url`, не попадает в URL или логи. CONNECT proxy находится только в выделенной серверной сети. HTTP Basic здесь защищает внутреннюю точку входа; эта сеть должна быть изолирована. При разнесении хостов использовать защищённый межсерверный транспорт.

Proxy принимает только `CONNECT canonical-host:443 HTTP/1.1`. Обычный forward HTTP, UDP, CONNECT-UDP, произвольные порты и nested CONNECT на другой прокси запрещены. Поддержка HTTPS/WSS сайта проходит через TCP/443. Нужные HTTP-only ресурсы сначала классифицируются: блокировка обязательной зависимости останавливает сценарий, не включает прямой доступ.

## 6. Последовательность одного соединения

```mermaid
sequenceDiagram
  participant B as Chromium
  participant R as Relay
  participant A as Agent
  participant H as hh.ru
  B->>R: CONNECT hh.ru:443 + lease credentials
  R->>R: проверить lease, policy, owner, epoch, quotas
  R->>A: OPEN(stream, host, port, data_ticket)
  A->>A: проверить локальную policy; resolve DNS
  A->>H: TCP connect к проверенному адресу
  A->>R: исходящий WSS /connect/v1/data + ticket
  A->>R: READY (первая text message)
  R->>B: 200 Connection Established
  B->>H: TLS handshake через R и A
  H-->>B: TLS response через A и R
```

`READY` содержит `stream_id`, `session_epoch`, `network_generation` и diagnostic address family. Relay посылает 200 только после валидного READY и подтверждённого агентом TCP connect. При любой неудаче до 200 — `502/503/504` и закрытие; browser request не был доказанно отправлен hh.ru.

TLS-байты идут Chromium→relay→agent→hh.ru и назад. Agent не декодирует HTTP body, не формирует заголовки hh.ru, не переписывает сертификаты и не исполняет JS сайта. HTTP/2 и WebSocket сайта работают внутри туннеля как обычные TCP bytes. QUIC/UDP в v1 отсутствует.

## 7. Data WebSocket framing

Endpoint: `GET /connect/v1/data`, subprotocol `leadscout-data.v1`, Authorization: data ticket. После handshake первая text message — READY не больше 1 KiB. Далее только binary messages:

```text
байт 0: kind
  0x00 DATA: остаток message = 1..65536 bytes
  0x01 FIN:  остаток пустой
  0x02 RST:  остаток = uint16 big-endian error code
```

Пустой DATA запрещён. WebSocket fragmentation обрабатывает библиотека, лимит message 65 537 bytes применяется до неограниченной аллокации. Compression/permessage-deflate отключён: внутри уже шифротекст. Ping/Pong/Close — стандартные управляющие WS frames библиотеки, не смешиваются с DATA.

`FIN` закрывает только направление записи соответствующего TCP-сокета; обратное направление может продолжить чтение. После двух FIN канал закрывается нормально. `RST`, неожиданный WS close, ошибка протокола или отмена рвут весь stream. FIN/RST всегда идут после ранее поставленных в очередь DATA. HALF-CLOSE deadline 10 секунд — стартовая настройка, затем RST.

Один data WS соответствует строго одному TCP-соединению и одному lease. Нет переназначения канала другому аккаунту и нет повторной доставки frames после reconnect. WS/TCP обеспечивает порядок внутри живого соединения; протокол не обещает exactly-once внешних HTTP-операций.

## 8. Flow control и стартовые лимиты

| Настройка | Начальное значение |
|---|---:|
| Control heartbeat | 25 секунд desktop/active mobile; без отдельного частого status polling |
| Offline detection | 3 пропущенных heartbeat, примерно 75 секунд |
| Connect к relay | 10 секунд |
| DNS и TCP к назначению | общий deadline 10 секунд |
| Полный OPEN/READY | 15 секунд |
| Неактивный TCP-stream | 120 секунд, настраивается после проверки сайта |
| Одновременные streams устройства | 16, pilot cap |
| Одновременные браузерные задачи устройства | 1; несколько его hh-аккаунтов обслуживаются очередью |
| DATA chunk | до 64 KiB |
| Очередь одного направления | максимум 2 chunks = 128 KiB |
| Память app-очередей stream | около 256 KiB без socket/TLS/library buffers |
| Reconnect | exponential backoff с full jitter: 1, 2, 4… до 60 секунд |

Чтение TCP останавливается при заполнении исходящей WS-очереди; чтение WS — при заполнении очереди записи TCP. Проверить, что WS-библиотека сама не создаёт неограниченный buffer; установить write-buffer caps. Дополнительные лимиты: общее число OPEN в ожидании, FD, суммарная память, байты/сек и байты за пользовательский сеанс.

Нет busy polling, нулевых fake-data для поддержания ОС и большого бессмысленного keepalive. Не заявляем измеренный расход батареи до аппаратных тестов.

## 9. Разрешённые назначения и DNS

Первая production policy содержит точные проверенные hostnames; `hh.ru:443` — только начальная точка, не утверждение, что этого достаточно для всех форм/капч. Зависимости определяются в тестовых сценариях из метаданных запросов. CDN/капчи включаются адресно после проверки принадлежности и необходимости. Нет `*.ru`, arbitrary user URL или открытого списка от backend.

Policy подписана отдельным ключом релизной политики; core проверяет её подпись, версию, срок и узкие верхние ограничения. Backend не может передать в OPEN новый произвольный hostname и тем самым расширить policy. Пользователь также может сузить policy. При несовпадении версий обновление/остановка, а не разрешение по умолчанию.

Проверки на relay и агенте:

1. Только hostname без credentials/path/query/fragment; canonical IDNA ASCII, lower-case; один terminal dot нормализовать, неоднозначные формы отклонить.
2. Точное совпадение hostname с allowlist и port=443.
3. IP literals, localhost, loopback, RFC1918, link-local, ULA, multicast, unspecified, metadata addresses запрещены.
4. Resolve выполняет агент через платформенный resolver. Проверяются все адреса и конечный адрес подключения; нельзя проверить DNS, а затем дать HTTP-клиенту повторно резолвить hostname.
5. Dial только к уже разрешённому адресу. Проверить IPv4-mapped IPv6, IPv6 scope IDs, несколько A/AAAA, изменение DNS и DNS rebinding.
6. Для NAT64 не блокировать весь синтезированный IPv6 диапазон: нормализовать известный/обнаруженный NAT64 prefix и проверить встроенный IPv4. Непонятный special-use адрес отклонять; поддержка IPv6-only сети — отдельный acceptance gate.
7. Старые сокеты не переиспользуются после network generation change.

При выборе Rust native resolver/dial интеграции отдельно проверить, что на телефоне они следуют выбранной сетевой политике и не делают независимый DNS с сервера. Агенту разрешён собственный DNS OS, но не произвольный DNS proxy для браузера.

TLS-поток непрозрачен: allowlist ограничивает адрес соединения, но не доказывает, что внутри выполняется только «отклик». Сервер автоматизации остаётся доверенной стороной, способной выполнять действия аккаунта. Для защиты от компрометации сервера агент ограничивает домены, срок, объём и право владельца; это не криптографическая верификация HTTP-действия.

## 10. State machines

### Устройство

```text
UNPAIRED → PAIRING → PAIRED_OFFLINE → CONNECTING → ONLINE_UNVERIFIED
ONLINE_UNVERIFIED → READY
READY → DRAINING → PAIRED_OFFLINE
READY → RECONNECTING → ONLINE_UNVERIFIED
любое привязанное состояние → REVOKED
```

`READY` требует: свежего control grant, подтверждённого режима ОС, актуальной policy, доступной сети, непустой квоты и успешного route probe. Готовность к конкретному заданию дополнительно проверяется при выдаче lease.

Чтобы избежать циклической зависимости READY↔probe, в ONLINE_UNVERIFIED разрешён специальный диагностический lease: только собственный probe-host, один stream, TTL 30 секунд, небольшой лимит данных. Бизнес-hosts ему недоступны. После успеха diagnostic lease закрывается, а новая бизнес-аренда выдаётся уже через обычный acquire.

### Маршрут задания

```text
WAITING_DEVICE → ACQUIRING → ACTIVE → DRAINING → CLOSED
                          ↘ FAILED
ACTIVE → LOST → CLOSED
```

После LOST выдаётся новый lease с новой session epoch; старый не оживляет stream. Использование старого secret/ID запрещено даже если устройство вернулось с тем же IP.

## 11. Обрывы, смена сети и fencing

Agent первым действием при смене Wi-Fi/сотовой сети увеличивает generation и закрывает target sockets; затем уведомляет relay. Потеря уведомления не критична: сокеты уже закрыты, heartbeat eventually обнаружит потерю. Новый путь должен пройти probe. TCP не мигрирует на другую сеть по предположению; не включать QUIC migration для внутренних потоков v1.

Операторский NAT способен менять адрес для новых соединений без события network change на устройстве. Стабильность публичного IP не гарантируется даже внутри одного Wi-Fi/LTE подключения. При явном обнаружении изменения маршрут переоценивается; это не обещание обнаружить все NAT-изменения до следующего запроса.

В relay допускается одна действующая control-session устройства. При конкурирующем reconnect backend поднимает epoch, отзывает старую сессию, relay закрывает её streams до активации новой. На нескольких relay это требует общей координации; в v1 один relay и один backend-authority.

Lease связывает `(owner, account, device, session_epoch, network_generation, route_revision)`. Проверка выполняется при CONNECT, OPEN и renew. Revocation закрывает существующие data sockets; одного запрета новых CONNECT недостаточно, поскольку HTTPS keep-alive может не создавать новый stream.

## 12. Без повторных внешних изменений

| Где оборвалось | Обработка backend |
|---|---|
| До создания браузера | WAITING_DEVICE, безопасная повторная подготовка |
| Чтение вакансии/поиск | Новый маршрут и повторное чтение после проверки сессии |
| Отправка OTP | Не слать OTP повторно автоматически; проверить состояние формы и запросить действие пользователя |
| Заполнение анкеты до submit | Сохранить черновик, создать новый контекст, повторно проверить схему |
| Нажатие submit началось или результат неизвестен | `ERROR_SUBMIT_UNCONFIRMED` / соответствующий `UNCERTAIN`; сначала read-only reconciliation |
| hh.ru подтвердил, запись в БД не удалась | Существующий `ERROR_LOCAL_PERSISTENCE`; не отправлять снова |
| Публикация/изменение резюме | Сверить существующий attempt и фактический профиль, не публиковать автоматически повторно |

TLS ACK, TCP FIN и сообщение агента «передано N bytes» не являются доказательством успешного отклика. Источник бизнес-истины — подтверждение hh.ru, зафиксированное сервером, и последующая проверка спорного результата.

При отмене пользователь должен знать: уже принятый hh.ru отклик нельзя отменить закрытием туннеля. Отмена прекращает дальнейшую работу и не стирает следы спорной операции.

## 13. Диагностика маршрута

Контролируемый `https://egress-check.<product-domain>/v1/probe` — отдельный узкий endpoint без redirect, arbitrary URL или fetch-функциональности. Браузер открывает его через тот же lease. Одноразовый nonce привязан к lease; сервис сохраняет наблюдаемый peer IP и возвращает подписанные nonce/time/address-family. Настройка доверенных reverse proxies не принимает произвольный клиентский X-Forwarded-For.

Agent отдельно может делать тот же probe напрямую для диагностики. Равенство двух адресов не обязательно при CGNAT, IPv4/IPv6 и per-destination routing. Поэтому production-проба доказывает работоспособность маршрута к probe-host; отсутствие серверного выхода доказывается firewall и тестами, а не только сравнением IP. Для реального hh.ru адрес назначения может быть другим.

Полный IP хранится только краткосрочно для диагностики по выбранной retention policy; продукту достаточно маскированного адреса и времени проверки. Политика хранения проектируется как часть данных пользователя.

## 14. Контракт общего core

```text
configure(endpoint, device_id, policy, limits)
start(session_descriptor)
stop(reason)
network_changed(generation, metered, availability)
set_quota(max_bytes, deadline)
sign_challenge(bytes) -> signature          [platform callback]
resolve_and_dial(host, port, network)       [platform socket adapter]
on_state(state, safe_error)
on_counters(rx, tx, stream_count)
on_batch_progress(completed, total, label)
```

Core владеет protocol validation, queues, socket pumping, epoch fencing и отменой. Оболочка владеет user consent, key store, UI, возможностью фонового исполнения и событиями ОС. Callback не вызывает синхронно ожидание на main thread; события доставляются через ограниченную очередь.

FFI v1: непрозрачные handles, bytes + lengths, явно определённый allocator/free, стабильные C ABI коды ошибок; без передачи Rust structs и исключений через границу. Swift/Kotlin bindings можно генерировать UniFFI, Windows — тонкий C ABI/PInvoke. Версия core встроена в signed package; динамическое скачивание исполняемого core не требуется.

## 15. Security acceptance

- Чужой account/device/lease не проходит ни один endpoint, включая list и status.
- Украденный короткий pairing code не достаточен без авторизованного владельца и подтверждения агентом.
- Истёкшие/replayed tickets и старые epochs отклоняются атомарно.
- Устройство не может самостоятельно выбрать чужой `user_id`.
- Domain allowlist нельзя расширить полем OPEN; нельзя подключиться к LAN/metadata.
- После revoke закрываются живые sockets и browser contexts, а не только control WS.
- Runtime relay не обладает ключом подписи allowlist или закрытыми ключами устройств.
- Протокольные логи не содержат DATA, Authorization, OTP, cookies, полного URL/письма/резюме.
- Компрометированное устройство не получает других пользователей; его ключ отзывается отдельно.
- Backend/admin-блокировка владельца отзывает leases всех его устройств через существующий AccessService.
- Companion не является открытым SOCKS/HTTP-сервером в домашней сети: у него нет входящего listener.

## 16. Ошибки, версии и обновление ключей

Единые machine-readable ошибки, без raw exception/response bodies:

| Код | Реакция |
|---|---|
| `DEVICE_OFFLINE` | Ожидание доступности устройства |
| `DEVICE_REVOKED` / `OWNER_DISABLED` | Терминальный запрет, не reconnect loop |
| `SESSION_EXPIRED` | Новая challenge-авторизация, старые streams закрываются |
| `PROTOCOL_UNSUPPORTED` | Требуется обновление клиента, никакого downgrade к plain TCP |
| `POLICY_OUTDATED` | Загрузить и проверить подписанную политику, иначе пауза |
| `DESTINATION_DENIED` | Не повторять автоматически; проверить необходимую зависимость |
| `DNS_FAILED` / `TARGET_CONNECT_FAILED` | Bounded retry подготовки чтения, не replay отправки |
| `NETWORK_POLICY_BLOCKED` | Показать Wi-Fi/cellular ограничение пользователя |
| `DEVICE_QUOTA_EXCEEDED` | Завершить/приостановить сеанс до разрешённого лимита |
| `OS_EXECUTION_STOPPED` | Новое действие пользователя или другой его выход |
| `SERVER_BUSY` | Освободить ресурсы, повторить постановку по not_before |
| `ROUTE_LOST` | Закрыть browser lease, определить uncertainty по attempt stage |

Control schema v1 версионируется независимо от app version. Сервер может поддерживать текущую и предыдущую безопасную версию в период обновления; минимальная поддерживаемая версия не задаётся непроверенным сообщением. Устаревший клиент получает причину и official update URL, не исполняемый код.

Key rotation:

- Ticket signer: несколько известных public `kid`, ограниченное перекрытие; старые короткие билеты истекают, новые подписываются новым ключом.
- Policy signer: trust anchor в приложении; новый ключ вводится обновлением приложения или cross-signed transition с заранее заданными правилами. Не принимать незнакомый ключ только потому, что он пришёл от relay.
- Device key: в v1 перепривязка с новым ключом и отзыв старого. Потерянный ключ не восстанавливать из backend.
- TLS/mTLS: штатная ротация сертификатов, alert до expiry, reconnect/drain; не pin один leaf certificate без запасного пути обновления.
- Time: TTL/challenge валидирует сервер; локальные idle/deadline timers используют monotonic clock. Неверные часы устройства могут ломать TLS и отображаются как диагностика, не выключение проверки сертификатов.
