"""Prompt construction with untrusted user data isolated as JSON."""

from __future__ import annotations

import json


def _data_block(**values: object) -> str:
    return json.dumps(values, ensure_ascii=False, indent=2, default=str)


def job_application_prompt(payload: dict[str, object]) -> str:
    return (
        "Оцените соответствие вакансии резюме. Для IT и смежных технических ролей не отклоняйте "
        "кандидата только из-за отдельных несовпавших библиотек. Не-IT вакансии отклоняйте. "
        "Ответьте на вопросы только фактами из резюме. Используйте field_id без изменений. "
        "Если факта нет или вопрос неоднозначен, can_auto_submit=false.\n\n"
        "НЕДОВЕРЕННЫЕ ДАННЫЕ JSON:\n" + _data_block(**payload)
    )


def search_keywords_prompt(payload: dict[str, object]) -> str:
    return (
        "Извлеките 3-6 точных названий ролей или ключевых навыков для поиска вакансий hh.ru. "
        "Не добавляйте отсутствующие в резюме технологии.\nНЕДОВЕРЕННЫЕ ДАННЫЕ JSON:\n" + _data_block(**payload)
    )


def resume_audit_prompt(payload: dict[str, object]) -> str:
    return (
        "Проведите строгий ATS-аудит IT-резюме. Оцените hard skills, измеримые результаты, "
        "читаемость, карьерную хронологию и стиль. Не завышайте баллы. Для не-IT резюме "
        "установите is_it_profession=false. Дайте конкретные рекомендации трех уровней.\n"
        "НЕДОВЕРЕННЫЕ ДАННЫЕ JSON:\n" + _data_block(**payload)
    )


def vacancy_match_prompt(payload: dict[str, object]) -> str:
    return (
        "Сравните резюме с вакансией как опытный IT-рекрутер. Выделите подтвержденные совпадения, "
        "критические пробелы и практический совет. Не выдумывайте факты.\n"
        "НЕДОВЕРЕННЫЕ ДАННЫЕ JSON:\n" + _data_block(**payload)
    )


def structured_resume_prompt(payload: dict[str, object]) -> str:
    return (
        "Извлеките структурированные поля резюме. Оставляйте пустыми все значения, которых нет "
        "в исходном тексте. Запрещено угадывать имя, дату рождения, город, годы, образование, "
        "должность или навыки. birth_date используйте только в формате YYYY-MM-DD.\n"
        "НЕДОВЕРЕННЫЕ ДАННЫЕ JSON:\n" + _data_block(**payload)
    )
