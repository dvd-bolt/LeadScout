"""QuestionnaireService business workflow."""

from __future__ import annotations

from typing import Any

from .common import (
    _CONFIRMABLE_QUESTIONNAIRE_STATES,
    _EDITABLE_QUESTIONNAIRE_STATES,
    _await,
    _json_object,
    _mapping,
    _Service,
)
from .errors import ServiceError


class QuestionnaireService(_Service):
    @staticmethod
    def _questions(item: dict) -> list[dict]:
        questions = _json_object(item.get("questions_json"), fallback=[])
        if not isinstance(questions, list):
            raise ServiceError("INVALID_INPUT", "Сохранённые вопросы анкеты повреждены.")
        return [question for question in questions if isinstance(question, dict)]

    @staticmethod
    def _answers(item: dict) -> list[dict]:
        payload = _json_object(item.get("ai_payload_json"), fallback={})
        if not isinstance(payload, dict) or not isinstance(payload.get("answers", []), list):
            raise ServiceError("INVALID_INPUT", "Сохранённые ответы анкеты повреждены.")
        return payload.get("answers", [])

    @staticmethod
    def _validate_answers(questions: list[dict], answers: list[Any], *, require_all: bool) -> list[dict]:
        by_id = {str(question.get("field_id")): question for question in questions if question.get("field_id")}
        normalized: list[dict] = []
        seen: set[str] = set()
        for raw in answers:
            try:
                answer = _mapping(raw)
            except TypeError as exc:
                raise ServiceError("INVALID_INPUT", "Ответ не соответствует вопросу анкеты.") from exc
            field_id = str(answer.get("field_id") or "")
            question = by_id.get(field_id)
            answer_type = str(answer.get("answer_type") or "")
            value = answer.get("value")
            values = value if isinstance(value, list) else [value]
            if (
                not question
                or field_id in seen
                or answer_type != question.get("answer_type", "text")
                or not values
                or any(
                    not isinstance(option, str) or not option.strip() or len(option) > 2_000
                    for option in values
                )
                or (isinstance(value, list) and answer_type != "checkbox")
            ):
                raise ServiceError("INVALID_INPUT", "Ответ не соответствует вопросу анкеты.")
            options = question.get("options") or []
            if answer_type in {"radio", "checkbox", "select"} and any(
                option not in options for option in values
            ):
                raise ServiceError("INVALID_INPUT", "Выберите предложенный вариант ответа.")
            seen.add(field_id)
            normalized.append(
                {
                    "field_id": field_id,
                    "answer_type": answer_type,
                    "value": (
                        [option.strip() for option in value]
                        if isinstance(value, list)
                        else value.strip()
                    ),
                }
            )
        if require_all:
            missing = [
                str(question.get("field_id"))
                for question in questions
                if question.get("required", True) and str(question.get("field_id")) not in seen
            ]
            if missing:
                raise ServiceError("INVALID_INPUT", "Заполните все обязательные поля анкеты.")
        return normalized

    async def _item(self, user_id: int, apply_id: int) -> dict:
        item = await _await(self.db.get_pending_questionnaire_for_user(user_id, apply_id))
        if not item:
            raise ServiceError("NOT_FOUND", "Анкета не найдена.")
        return item

    async def edit(
        self,
        user_id: int,
        apply_id: int,
        cover_letter: str | None = None,
        answers: list[Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict:
        item = await self._item(user_id, apply_id)
        if expected_revision is not None and int(item.get("revision", 0)) != expected_revision:
            raise ServiceError("CONFLICT", "Анкета была изменена. Обновите данные перед сохранением.")
        if item.get("status") not in _EDITABLE_QUESTIONNAIRE_STATES:
            raise ServiceError("CONFLICT", "Анкета уже обрабатывается.")
        if cover_letter is not None and (not isinstance(cover_letter, str) or len(cover_letter) > 10_000):
            raise ServiceError("INVALID_INPUT", "Сопроводительное письмо слишком длинное.")
        normalized = None
        if answers is not None:
            if not isinstance(answers, list) or len(answers) > 50:
                raise ServiceError("INVALID_INPUT", "Слишком много ответов в анкете.")
            normalized = self._validate_answers(self._questions(item), answers, require_all=False)
        edit_kwargs = {"return_item": True}
        if expected_revision is not None:
            edit_kwargs["expected_revision"] = expected_revision
        saved = await _await(
            self.db.edit_pending_questionnaire(
                user_id, apply_id, cover_letter, normalized, **edit_kwargs
            )
        )
        if not saved:
            raise ServiceError("CONFLICT", "Анкета уже обрабатывается.")
        return saved

    async def confirm(self, user_id: int, apply_id: int, expected_revision: int | None = None) -> dict:
        item = await self._item(user_id, apply_id)
        revision = int(item.get("revision", 0))
        if expected_revision is not None and expected_revision != revision:
            raise ServiceError("CONFLICT", "Анкета была изменена. Обновите данные перед отправкой.")
        if item.get("status") == "SUBMITTING":
            is_running = getattr(self.coordinator, "is_questionnaire_running", None)
            if is_running and is_running(user_id, apply_id):
                return {"apply_id": int(apply_id), "status": "ALREADY_RUNNING"}
            raise ServiceError("CONFLICT", "Анкета уже обрабатывается.")
        if item.get("status") not in _CONFIRMABLE_QUESTIONNAIRE_STATES:
            raise ServiceError("CONFLICT", "Анкета недоступна для отправки.")
        answers = self._answers(item)
        self._validate_answers(self._questions(item), answers, require_all=True)
        state = str(await _await(self.coordinator.start_questionnaire(user_id, apply_id, expected_revision=revision)))
        if state == "ALREADY_RUNNING":
            return {"apply_id": int(apply_id), "status": state}
        if state != "STARTED":
            raise ServiceError("CONFLICT", "Анкета недоступна для отправки.")
        return {"apply_id": int(apply_id), "status": state}

    async def skip(self, user_id: int, apply_id: int) -> dict:
        original = await self._item(user_id, apply_id)
        skip = getattr(self.db, "skip_pending_questionnaire", None)
        if skip is None:
            raise ServiceError("CONFLICT", "Хранилище пока не поддерживает пропуск анкеты.")
        result = await _await(skip(user_id, apply_id))
        status = str(result.get("status")) if isinstance(result, dict) else str(result)
        if status == "NOT_FOUND":
            raise ServiceError("NOT_FOUND", "Анкета не найдена.")
        if status == "CONFLICT":
            raise ServiceError("CONFLICT", "Анкета уже обрабатывается.")
        if status != "SKIPPED":
            raise ServiceError("CONFLICT", "Не удалось пропустить анкету.")
        item = await _await(self.db.get_pending_questionnaire_for_user(user_id, apply_id))
        if item:
            return item
        return {**original, "status": "SKIPPED"}
