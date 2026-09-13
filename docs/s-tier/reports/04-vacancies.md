# Модуль 04 — устойчивое извлечение вакансий

Status: VERIFIED_LOCAL. Живая проверка hh.ru не выполнялась.

## Изменения

- Один DOM-парсер обслуживает отклики и загрузку вакансии для аудита.
- Сохранены `title`, `company`, `description`, `url`; добавлены skills,
  experience, исходная salary/salary_text, from/to, currency, period, gross,
  work format, location, responsibilities и requirements.
- Неизвестные значения остаются `null`. Зарплата не сводит разные валюты или
  периоды к сравнимым числам; исходная строка сохраняется без преобразования.
- Явно различаются корректная, архивная, CAPTCHA, login, внешняя и неполная
  страница. Ожидаются видимые title и description в пределах одного дедлайна.
- Финальный URL и ID вакансии проверяются. Поиск ожидает карточки, сохраняет
  настройки и stop-слова, нормализует URL и устраняет дубли по vacancy ID.
- `ERROR_INCOMPLETE` и новые наблюдаемые исходы имеют безопасные причины в
  диагностике; внешний переход отклика остаётся `SKIPPED_EXTERNAL`.

## Контракт для модуля 5

Успешный объект имеет `status: SUCCESS`; любой иной `status` нельзя заменять
пустым description. Новые поля описаны в `STATE.md`; это данные rendered DOM,
не результат ИИ. Потребители откликов и аудита продолжают использовать общий
`extract_vacancy_details` и совместимые базовые поля.

## Проверки

- `pytest tests/test_vacancy_extraction.py` — 11 passed.
- `pytest tests/test_application_diagnostics.py` — 9 passed.
- `pytest tests/test_tasks_and_browser.py` — 12 passed.
- `pytest tests/test_audit_regressions.py` — 35 passed.
- Профильный Ruff и compileall — успешно.

Фикстуры обезличены и покрывают варианты карточек, задержанный DOM, отсутствующие
поля, зарплаты, блокировки, внешний URL и дубликаты. Они не подтверждают
актуальную live-вёрстку hh.ru.

## Финальная перепроверка

- CAPTCHA определяется по видимому контролу или явной фразе, а не по любому
  слову CAPTCHA внутри корректного описания вакансии.
- CAPTCHA-редирект hh.ru распознаётся раньше проверки несовпавшего vacancy URL.
- Обезличенные parser fixtures, Ruff и полный pytest успешны: 276 passed.
