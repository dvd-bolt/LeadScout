# Параллельные контракты LeadScout

## Протокол готовности

Каждый модуль публикует `Protocol: parallel-v1` и один из статусов
`WORKING`, `READY`, `INTEGRATING`, `COMPLETE` или `BLOCKED` в своём отчёте.
После `READY` владелец модуля не меняет переданную часть без повторного статуса
`WORKING` и новой передачи.

## Хранилище

- `skip_pending_questionnaire(user_id, apply_id) -> str` возвращает
  `SKIPPED`, `NOT_FOUND` или `CONFLICT`; повторный пропуск `SKIPPED` успешен.
- `set_operation_needs_input(operation_id, user_id, result) -> bool` переводит
  `RUNNING` в `NEEDS_INPUT` и сохраняет результат.

## Сервисы

`leadscout.services.facade.build_services(*, db, coordinator, login_manager,
resume_manager, ai=None)` создаёт единый фасад с группами `accounts`, `resumes`,
`automation`, `questionnaires`, `audits`.

`ServiceError` содержит `code` и `message`. Транспортное отображение:
`NOT_FOUND` → 404, `CONFLICT` → 409, `INVALID_INPUT` → 422,
`LIMIT_REACHED` → 400. `AuditSource` фиксирует `user_id`, `account_id`,
`resume_snapshot_id` и исходный `resume_text` до запуска фонового аудита.

## API и операции

Все прикладные маршруты находятся под `/api/v1`; владелец определяется по
Telegram-подписи и подписанной cookie-сессии. Изменяющие запросы требуют
допустимый Origin и CSRF-токен. Операции проходят состояния `PENDING` →
`RUNNING` → `SUCCEEDED`, `FAILED` или `NEEDS_INPUT`. Импорт с
`result.status == "NEEDS_FIELDS"` сохраняется как `NEEDS_INPUT`; продолжение —
новый multipart-запрос с файлом и `structured_json`.

## Mini App и бот

Канонические параметры прямых переходов: `target`, `account_id`, `apply_id`,
`snapshot_id`; query располагается до hash-route. Переход только открывает экран
проверки, не выполняет подтверждение, удаление или запуск. Остановка остаётся
прямым вызовом сервиса.
