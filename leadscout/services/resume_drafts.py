"""Resume draft, PDF prefill, preflight, and durable publication workflow."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date
from typing import Any

from pydantic import ValidationError

from leadscout.core import config
from leadscout.documents.pdf_reader import PDFValidationError, extract_text_from_pdf
from leadscout.integrations.ai.client import AIServiceError
from leadscout.models.resume_drafts import ResumeDraftData, empty_resume_draft
from leadscout.models.resumes import StructuredResume
from leadscout.storage.repositories.resume_drafts import DraftRevisionConflict

from .common import _await, _mapping, _Service
from .errors import ServiceError


def _fingerprint(value: Any) -> str:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _merge_missing(current: Any, extracted: Any) -> Any:
    """Add extracted values without overwriting any explicit user value."""
    if isinstance(current, dict) and isinstance(extracted, dict):
        keys = current.keys() | extracted.keys()
        return {key: _merge_missing(current.get(key), extracted.get(key)) for key in keys}
    if isinstance(current, list):
        return current if current else (extracted or [])
    if isinstance(current, bool):
        return current
    if current not in (None, ""):
        return current
    return extracted if extracted is not None else current


def structured_to_draft(structured: StructuredResume) -> dict:
    skills = [{"name": skill, "level": ""} for skill in structured.skills]
    return ResumeDraftData.model_validate(
        {
            "profession": {"title": structured.title},
            "personal": {
                "first_name": structured.first_name,
                "last_name": structured.last_name,
                "middle_name": structured.middle_name,
                "birth_date": structured.birth_date,
                "city": structured.city,
            },
            "work_conditions": {"salary": structured.salary},
            "skills": skills,
            "experiences": [item.model_dump(exclude_none=True) for item in structured.experiences],
            "education": [item.model_dump() for item in structured.education],
            "about": {"text": structured.about},
        }
    ).model_dump()


def draft_to_structured(data: ResumeDraftData) -> StructuredResume:
    return StructuredResume.model_validate(
        {
            "first_name": data.personal.first_name,
            "last_name": data.personal.last_name,
            "middle_name": data.personal.middle_name,
            "birth_date": data.personal.birth_date,
            "title": data.profession.title,
            "salary": data.work_conditions.salary,
            "city": data.personal.city,
            "experiences": [item.model_dump(exclude={"selected"}) for item in data.experiences if item.selected],
            "education": [item.model_dump(exclude={"selected"}) for item in data.education if item.selected],
            "skills": [item.name for item in data.skills if item.name],
            "about": data.about.text,
        }
    )


def validate_draft(data: ResumeDraftData) -> dict:
    errors: list[dict[str, str]] = []

    def required(path: str, value: str, message: str) -> None:
        if not value.strip():
            errors.append({"path": path, "code": "REQUIRED", "message": message})

    required("profession.title", data.profession.title, "Укажите название резюме.")
    required("profession.hh_profession", data.profession.hh_profession, "Подтвердите профессию из справочника hh.ru.")
    required("personal.first_name", data.personal.first_name, "Укажите имя.")
    required("personal.last_name", data.personal.last_name, "Укажите фамилию.")
    required("personal.city", data.personal.city, "Укажите город.")
    required("personal.birth_date", data.personal.birth_date, "Укажите точную дату рождения.")
    if data.personal.birth_date:
        try:
            date.fromisoformat(data.personal.birth_date)
        except ValueError:
            errors.append(
                {"path": "personal.birth_date", "code": "INVALID_DATE", "message": "Дата должна быть в формате ГГГГ-ММ-ДД."}
            )
    for index, item in enumerate(data.experiences):
        if not item.selected:
            continue
        for name, value, label in (
            ("company", item.company, "компанию"),
            ("position", item.position, "должность"),
            ("start_month", item.start_month, "месяц начала"),
            ("start_year", item.start_year, "год начала"),
        ):
            required(f"experiences.{index}.{name}", value, f"Укажите {label} для места работы №{index + 1}.")
        if not item.is_current:
            required(f"experiences.{index}.end_month", item.end_month, f"Укажите месяц окончания работы №{index + 1}.")
            required(f"experiences.{index}.end_year", item.end_year, f"Укажите год окончания работы №{index + 1}.")
    for index, item in enumerate(data.education):
        if not item.selected:
            continue
        required(f"education.{index}.institution", item.institution, f"Укажите учебное заведение №{index + 1}.")
        required(f"education.{index}.end_year", item.end_year, f"Укажите год окончания обучения №{index + 1}.")
    return {"valid": not errors, "field_errors": errors}


def compare_profile(data: ResumeDraftData, profile: dict) -> list[dict]:
    checks = (
        ("personal.first_name", data.personal.first_name, profile.get("first_name")),
        ("personal.last_name", data.personal.last_name, profile.get("last_name")),
        ("personal.birth_date", data.personal.birth_date, profile.get("birth_date")),
        ("personal.city", data.personal.city, profile.get("city")),
        ("contacts.phone", data.contacts.phone, profile.get("phone")),
        ("contacts.email", data.contacts.email, profile.get("email")),
    )
    conflicts = []
    for path, draft_value, profile_value in checks:
        if draft_value and profile_value and str(draft_value).strip().casefold() != str(profile_value).strip().casefold():
            conflicts.append({"path": path, "draft_value": draft_value, "profile_value": profile_value})
    return conflicts


class ResumeDraftService(_Service):
    def __init__(self, *, db: Any, coordinator: Any, resume_manager: Any, ai: Any):
        super().__init__(db=db, coordinator=coordinator)
        self.resume_manager = resume_manager
        self.ai = ai

    async def _draft(self, user_id: int, account_id: int, draft_id: int) -> dict:
        draft = await _await(self.db.get_resume_draft(user_id, account_id, draft_id))
        if not draft:
            raise ServiceError("NOT_FOUND", "Черновик резюме не найден.")
        return draft

    @staticmethod
    def _require_editable(draft: dict) -> None:
        if draft["status"] == "COMPLETED":
            raise ServiceError(
                "CONFLICT", "Завершённый черновик доступен только для просмотра. Создайте новый."
            )
        if draft["status"] == "PUBLISHING":
            raise ServiceError("CONFLICT", "Дождитесь завершения текущей публикации.")

    async def create(self, user_id: int, account_id: int, source: str, data: dict | None = None) -> dict:
        await self._account(user_id, account_id)
        source = source.upper()
        if source not in {"MANUAL", "PDF"}:
            raise ServiceError("INVALID_INPUT", "Неизвестный способ создания резюме.")
        try:
            payload = ResumeDraftData.model_validate(data or empty_resume_draft()).model_dump()
        except ValidationError as exc:
            raise ServiceError("INVALID_INPUT", "Данные черновика не прошли проверку.") from exc
        return _mapping(await _await(self.db.create_resume_draft(user_id, account_id, source, payload)))

    async def list(self, user_id: int, account_id: int) -> list[dict]:
        await self._account(user_id, account_id)
        return list(await _await(self.db.list_resume_drafts(user_id, account_id)))

    async def get(self, user_id: int, account_id: int, draft_id: int) -> dict:
        return await self._draft(user_id, account_id, draft_id)

    async def update(
        self,
        user_id: int,
        account_id: int,
        draft_id: int,
        expected_revision: int,
        data: dict,
        current_step: str,
    ) -> dict:
        try:
            payload = ResumeDraftData.model_validate(data).model_dump()
        except ValidationError as exc:
            raise ServiceError("INVALID_INPUT", "Черновик содержит некорректные данные.") from exc
        self._require_editable(await self._draft(user_id, account_id, draft_id))
        try:
            result = await _await(
                self.db.update_resume_draft(
                    user_id, account_id, draft_id, expected_revision, payload, current_step
                )
            )
        except DraftRevisionConflict as exc:
            raise ServiceError("CONFLICT", "Черновик уже изменён в другой вкладке. Обновите данные.") from exc
        if not result:
            raise ServiceError("NOT_FOUND", "Черновик резюме не найден.")
        return _mapping(result)

    async def delete(self, user_id: int, account_id: int, draft_id: int) -> None:
        draft = await self._draft(user_id, account_id, draft_id)
        if draft["status"] == "PUBLISHING":
            raise ServiceError("CONFLICT", "Нельзя удалить черновик во время публикации.")
        if not await _await(self.db.delete_resume_draft(user_id, account_id, draft_id)):
            raise ServiceError("CONFLICT", "Черновик сейчас нельзя удалить.")

    async def extract_pdf(self, user_id: int, account_id: int, draft_id: int, pdf_path: str) -> dict:
        draft = await self._draft(user_id, account_id, draft_id)
        self._require_editable(draft)
        await _await(self.db.set_resume_draft_status(user_id, account_id, draft_id, "PARSING"))
        try:
            text = await asyncio.to_thread(extract_text_from_pdf, pdf_path)
            if hasattr(self.ai, "extract_resume_draft"):
                extracted_model = await self.ai.extract_resume_draft(text, strict=True)
                extracted = extracted_model.model_dump()
                # A profession from a PDF is free text, not a confirmed hh
                # dictionary choice. The user must resolve it explicitly.
                extracted["profession"]["hh_profession"] = ""
                extracted["profession"]["hh_profession_id"] = ""
            else:
                structured = await self.ai.extract_full_structured_resume(text, strict=True)
                extracted = structured_to_draft(structured)
            merged = ResumeDraftData.model_validate(_merge_missing(draft["data"], extracted))
            validation = validate_draft(merged)
            status = "READY" if validation["valid"] else "NEEDS_INPUT"
            updated = await _await(
                self.db.replace_resume_draft_data(
                    user_id,
                    account_id,
                    draft_id,
                    draft["revision"],
                    merged.model_dump(),
                    status=status,
                    validation=validation,
                )
            )
            if not updated:
                raise ServiceError("NOT_FOUND", "Черновик резюме не найден.")
            return {
                "status": "SUCCESS",
                "code": "PDF_PARSED",
                "message": "PDF распознан. Проверьте заполненные данные.",
                "draft_id": draft_id,
                "revision": updated["revision"],
                "field_errors": validation["field_errors"],
            }
        except PDFValidationError as exc:
            await _await(self.db.set_resume_draft_status(user_id, account_id, draft_id, "FAILED"))
            return {"status": "ERROR", "code": "PDF_INVALID", "stage": "EXTRACT", "message": str(exc), "retryable": False}
        except AIServiceError:
            await _await(self.db.set_resume_draft_status(user_id, account_id, draft_id, "NEEDS_INPUT"))
            return {
                "status": "NEEDS_INPUT",
                "code": "AI_UNAVAILABLE",
                "stage": "PARSE",
                "message": "ИИ сейчас недоступен. Повторите распознавание или заполните черновик вручную.",
                "required_action": "RETRY_OR_FILL_MANUALLY",
                "retryable": True,
            }
        except (ValidationError, TypeError, ValueError):
            await _await(self.db.set_resume_draft_status(user_id, account_id, draft_id, "NEEDS_INPUT"))
            return {
                "status": "NEEDS_INPUT",
                "code": "AI_RESPONSE_INVALID",
                "stage": "PARSE",
                "message": "Распознанные данные имеют неверный формат. Заполните черновик вручную или повторите позже.",
                "required_action": "RETRY_OR_FILL_MANUALLY",
                "retryable": True,
            }

    async def validate(self, user_id: int, account_id: int, draft_id: int) -> dict:
        draft = await self._draft(user_id, account_id, draft_id)
        self._require_editable(draft)
        data = ResumeDraftData.model_validate(draft["data"])
        validation = validate_draft(data)
        updated = await _await(
            self.db.set_resume_draft_status(
                user_id,
                account_id,
                draft_id,
                "READY" if validation["valid"] else "NEEDS_INPUT",
                validation=validation,
            )
        )
        return {**validation, "status": updated["status"] if updated else "FAILED", "revision": draft["revision"]}

    async def preflight(self, user_id: int, account_id: int, draft_id: int) -> dict:
        draft = await self._draft(user_id, account_id, draft_id)
        self._require_editable(draft)
        data = ResumeDraftData.model_validate(draft["data"])
        validation = validate_draft(data)
        if not validation["valid"]:
            await _await(
                self.db.set_resume_draft_status(
                    user_id, account_id, draft_id, "NEEDS_INPUT", validation=validation
                )
            )
            return {
                "status": "NEEDS_INPUT",
                "code": "VALIDATION_FAILED",
                "stage": "VALIDATE",
                **validation,
                "required_action": "EDIT_DRAFT",
            }
        await self._external_account(user_id, account_id)
        result = _mapping(await _await(self.resume_manager.inspect_resume_profile(user_id, account_id)))
        if result.get("status") != "SUCCESS":
            return result
        profile = dict(result.get("profile") or {})
        conflicts = compare_profile(data, profile)
        preflight = {
            "profile": profile,
            "profile_fingerprint": str(result.get("profile_fingerprint") or _fingerprint(profile)),
            "conflicts": conflicts,
            "profile_changes": conflicts,
            "capabilities": result.get("capabilities") or {},
        }
        fingerprint = _fingerprint({"revision": draft["revision"], "preflight": preflight})
        status = "NEEDS_REVIEW" if conflicts else "READY"
        await _await(
            self.db.save_resume_preflight(
                user_id, account_id, draft_id, draft["revision"], preflight, fingerprint, status
            )
        )
        return {
            "status": status,
            "code": "PROFILE_CONFLICTS" if conflicts else "PREFLIGHT_READY",
            "stage": "PREFLIGHT",
            "revision": draft["revision"],
            "confirmation_fingerprint": fingerprint,
            **preflight,
            "required_action": "CONFIRM_CONFLICTS" if conflicts else None,
        }

    async def register_publish(
        self,
        user_id: int,
        account_id: int,
        draft_id: int,
        expected_revision: int,
        idempotency_key: str,
        confirmation_fingerprint: str,
    ) -> tuple[dict, bool]:
        draft = await self._draft(user_id, account_id, draft_id)
        self._require_editable(draft)
        if draft["revision"] != expected_revision:
            raise ServiceError("CONFLICT", "Черновик изменился после проверки. Выполните проверку ещё раз.")
        if draft.get("preflight_revision") != expected_revision or not draft.get("preflight_fingerprint"):
            raise ServiceError("CONFLICT", "Сначала выполните проверку данных и профиля hh.ru.")
        conflicts = list((draft.get("preflight") or {}).get("conflicts") or [])
        if conflicts and confirmation_fingerprint != draft["preflight_fingerprint"]:
            raise ServiceError("CONFLICT", "Подтвердите показанные изменения профиля для этой версии черновика.")
        if not idempotency_key or len(idempotency_key) > 200:
            raise ServiceError("INVALID_INPUT", "Некорректный ключ публикации.")
        try:
            return await _await(
                self.db.create_resume_publish_attempt(
                    user_id,
                    account_id,
                    draft_id,
                    expected_revision,
                    idempotency_key,
                    confirmation_fingerprint,
                )
            )
        except DraftRevisionConflict as exc:
            raise ServiceError("CONFLICT", "Публикация этой версии уже начата или черновик изменился.") from exc

    async def run_publish(self, user_id: int, attempt_id: str) -> dict:
        attempt = await _await(self.db.get_resume_publish_attempt(user_id, attempt_id))
        if not attempt:
            raise ServiceError("NOT_FOUND", "Попытка публикации не найдена.")
        draft = await self._draft(user_id, attempt["account_id"], attempt["draft_id"])
        await _await(
            self.db.update_resume_publish_attempt(
                user_id, attempt_id, stage="OPEN", status="PUBLISHING", result={}
            )
        )
        await _await(
            self.db.record_resume_publish_event(
                attempt_id, "OPEN", "STARTED", {"app_version": config.APP_VERSION}
            )
        )

        current_profile = _mapping(
            await _await(
                self.resume_manager.inspect_resume_profile(user_id, attempt["account_id"])
            )
        )
        if current_profile.get("status") != "SUCCESS":
            result = {**current_profile, "stage": "PREFLIGHT_RECHECK"}
            await _await(
                self.db.update_resume_publish_attempt(
                    user_id,
                    attempt_id,
                    stage="PREFLIGHT_RECHECK",
                    status="NEEDS_ACTION",
                    result=result,
                    draft_status="NEEDS_REVIEW",
                )
            )
            return {**result, "attempt_id": attempt_id}
        previous_profile_fingerprint = str(
            (draft.get("preflight") or {}).get("profile_fingerprint") or ""
        )
        if str(current_profile.get("profile_fingerprint") or "") != previous_profile_fingerprint:
            result = {
                "status": "NEEDS_REVIEW",
                "code": "PROFILE_CHANGED",
                "stage": "PREFLIGHT_RECHECK",
                "message": "Профиль hh.ru изменился после проверки. Сравните данные ещё раз.",
                "required_action": "RUN_PREFLIGHT_AGAIN",
            }
            await _await(
                self.db.update_resume_publish_attempt(
                    user_id,
                    attempt_id,
                    stage="PREFLIGHT_RECHECK",
                    status="FAILED",
                    result=result,
                    draft_status="NEEDS_REVIEW",
                )
            )
            return {**result, "attempt_id": attempt_id}

        async def external_saved(hh_resume_id: str, hh_resume_url: str) -> None:
            await _await(
                self.db.update_resume_publish_attempt(
                    user_id,
                    attempt_id,
                    stage="EXTERNAL_CREATED",
                    status="PUBLISHING",
                    result={"external_saved": True},
                    hh_resume_id=hh_resume_id,
                    hh_resume_url=hh_resume_url,
                )
            )
            await _await(
                self.db.record_resume_publish_event(
                    attempt_id, "EXTERNAL_CREATED", "HH_RESUME_ID_SAVED", {"external_saved": True}
                )
            )

        result = _mapping(
            await _await(
                self.resume_manager.publish_resume_draft(
                    user_id,
                    attempt["account_id"],
                    draft["data"],
                    attempt=attempt,
                    on_external_saved=external_saved,
                )
            )
        )
        status = str(result.get("status") or "ERROR")
        if status == "SUCCESS":
            attempt_status, draft_status = "COMPLETED", "COMPLETED"
        elif status == "PARTIAL":
            attempt_status, draft_status = "PARTIAL", "NEEDS_INPUT"
        elif status in {"NEEDS_ACTION", "NEEDS_INPUT"}:
            attempt_status, draft_status = "NEEDS_ACTION", "NEEDS_INPUT"
        elif status == "UNCERTAIN":
            attempt_status, draft_status = "UNCERTAIN", "NEEDS_REVIEW"
        else:
            attempt_status, draft_status = "FAILED", "FAILED"
        await _await(
            self.db.update_resume_publish_attempt(
                user_id,
                attempt_id,
                stage=str(result.get("stage") or "VERIFY"),
                status=attempt_status,
                result=result,
                hh_resume_id=str(result.get("hh_resume_id") or ""),
                hh_resume_url=str(result.get("hh_resume_url") or ""),
                draft_status=draft_status,
            )
        )
        await _await(
            self.db.record_resume_publish_event(
                attempt_id,
                str(result.get("stage") or "VERIFY"),
                str(result.get("code") or status),
                {
                    "status": status,
                    "external_saved": bool(result.get("hh_resume_id")),
                    "app_version": config.APP_VERSION,
                    "recognized_screens": list(result.get("recognized_screens") or []),
                },
            )
        )
        return {**result, "attempt_id": attempt_id}

    async def reconcile(self, user_id: int, account_id: int, draft_id: int) -> dict:
        draft = await self._draft(user_id, account_id, draft_id)
        attempt = await _await(self.db.get_latest_resume_publish_attempt(user_id, account_id, draft_id))
        if not attempt:
            raise ServiceError("NOT_FOUND", "Для черновика нет попытки публикации.")
        result = _mapping(
            await _await(self.resume_manager.reconcile_resume_publish(user_id, account_id, draft, attempt))
        )
        status = str(result.get("status") or "UNCERTAIN")
        if status == "SUCCESS":
            attempt_status, draft_status = "COMPLETED", "COMPLETED"
        elif status == "PARTIAL":
            attempt_status, draft_status = "PARTIAL", "NEEDS_INPUT"
        else:
            attempt_status, draft_status = "UNCERTAIN", "NEEDS_REVIEW"
        await _await(
            self.db.update_resume_publish_attempt(
                user_id,
                attempt["id"],
                stage="RECONCILE",
                status=attempt_status,
                result=result,
                hh_resume_id=str(result.get("hh_resume_id") or ""),
                hh_resume_url=str(result.get("hh_resume_url") or ""),
                draft_status=draft_status,
            )
        )
        return {**result, "attempt_id": attempt["id"]}

    async def resume(self, user_id: int, account_id: int, draft_id: int) -> dict:
        attempt = await _await(self.db.get_latest_resume_publish_attempt(user_id, account_id, draft_id))
        if not attempt:
            raise ServiceError("NOT_FOUND", "Незавершённая публикация не найдена.")
        if attempt["status"] not in {"NEEDS_ACTION", "UNCERTAIN", "PARTIAL"}:
            return {"status": attempt["status"], "attempt_id": attempt["id"], **attempt.get("result", {})}
        return await self.run_publish(user_id, attempt["id"])


__all__ = [
    "ResumeDraftService",
    "compare_profile",
    "draft_to_structured",
    "structured_to_draft",
    "validate_draft",
]
