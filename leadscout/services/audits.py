"""AuditService business workflow."""

from __future__ import annotations

from typing import Any

from utils.validation import normalize_hh_vacancy_url

from .common import _await, _mapping, _Service
from .contracts import AuditSource
from .errors import ServiceError


class AuditService(_Service):
    def __init__(
        self,
        *,
        db: Any,
        coordinator: Any,
        resume_manager: Any,
        ai: Any,
    ):
        super().__init__(db=db, coordinator=coordinator)
        self.resume_manager = resume_manager
        self.ai = ai

    async def prepare(
        self,
        user_id: int,
        account_id: int | None = None,
        resume_snapshot_id: int | None = None,
        resume_text: str | None = None,
    ) -> AuditSource:
        account = None
        snapshot = None
        if account_id is not None:
            account = await self._account(user_id, account_id)

        if resume_snapshot_id is not None:
            snapshot = await _await(self.db.get_resume_snapshot_for_user(user_id, resume_snapshot_id))
            if not snapshot:
                raise ServiceError("NOT_FOUND", "Резюме не найдено.")
            if account and int(snapshot.get("account_id", -1)) != int(account["id"]):
                raise ServiceError("NOT_FOUND", "Резюме не найдено.")
            if not account:
                account_id = int(snapshot["account_id"])
                account = await self._account(user_id, account_id)
        elif not account and hasattr(self.db, "get_active_account"):
            account = await _await(self.db.get_active_account(user_id))

        if not snapshot and resume_text is None and account:
            snapshot = await _await(self.db.get_active_resume_snapshot(user_id, int(account["id"])))

        text = (
            resume_text
            if resume_text is not None
            else (snapshot or {}).get("extracted_text") or (account or {}).get("resume_text") or ""
        )
        if not isinstance(text, str) or not 50 <= len(text.strip()) <= 50_000:
            raise ServiceError(
                "INVALID_INPUT" if text.strip() else "CONFLICT",
                "Для аудита нужен текст резюме длиной от 50 до 50000 символов.",
            )
        return AuditSource(
            user_id=int(user_id),
            account_id=int(account["id"]) if account else account_id,
            resume_snapshot_id=(int(snapshot["id"]) if snapshot and snapshot.get("id") is not None else None),
            resume_text=text.strip(),
        )

    async def run(self, source: AuditSource) -> dict:
        if not isinstance(source, AuditSource):
            raise ServiceError("INVALID_INPUT", "Источник аудита указан неверно.")
        try:
            result = await _await(self.ai.analyze_resume_quality(source.resume_text))
            audit = _mapping(result)
        except Exception as exc:
            return {"status": "ERROR", "message": str(exc) or "ИИ-сервис временно недоступен."}
        if not audit.get("is_it_profession"):
            return {
                "status": "ERROR",
                "message": audit.get("rejection_reason") or "Аудит поддерживает только IT-резюме.",
            }

        try:
            insights = [
                _mapping(item) if not isinstance(item, dict) else dict(item) for item in (audit.get("insights") or [])
            ]
            audit_id = await _await(
                self.db.save_resume_audit(
                    source.user_id,
                    source.account_id,
                    str(audit.get("profession_name") or ""),
                    int(audit.get("overall_score") or 0),
                    dict(audit.get("category_scores") or {}),
                    list(audit.get("penalties") or []),
                    list(audit.get("top_recommendations") or []),
                    insights,
                    str(audit.get("summary_text") or ""),
                    source_resume_text=source.resume_text,
                    source_resume_snapshot_id=source.resume_snapshot_id,
                )
            )
        except Exception:
            return {"status": "ERROR", "message": "Не удалось сохранить результат аудита."}
        return {
            "status": "SUCCESS",
            "audit_id": audit_id,
            "is_it_profession": bool(audit["is_it_profession"]),
        }

    async def match(
        self,
        user_id: int,
        audit_id: int,
        vacancy_text: str | None = None,
        vacancy_url: str | None = None,
    ) -> dict:
        text_supplied = isinstance(vacancy_text, str) and bool(vacancy_text.strip())
        url_supplied = isinstance(vacancy_url, str) and bool(vacancy_url.strip())
        if text_supplied == url_supplied:
            raise ServiceError("INVALID_INPUT", "Укажите ровно один источник вакансии: текст или ссылку.")
        audit = await _await(self.db.get_resume_audit_for_user(user_id, audit_id))
        if not audit or not str(audit.get("source_resume_text") or "").strip():
            raise ServiceError("NOT_FOUND", "Исходный документ аудита не найден.")

        description = vacancy_text.strip() if text_supplied else ""
        if text_supplied and not 15 <= len(description) <= 50_000:
            raise ServiceError("INVALID_INPUT", "Описание вакансии должно содержать от 15 до 50000 символов.")
        if url_supplied:
            normalized = normalize_hh_vacancy_url(vacancy_url or "")
            if not normalized:
                raise ServiceError("INVALID_INPUT", "Допустима только HTTPS-ссылка на вакансию hh.ru.")
            account = None
            linked_account_id = audit.get("account_id")
            if linked_account_id is not None:
                account = await _await(self.db.get_account_for_user(user_id, int(linked_account_id)))
            elif hasattr(self.db, "get_active_account"):
                account = await _await(self.db.get_active_account(user_id))
            if (
                not account
                or account.get("session_status") != "ACTIVE"
                or ("encrypted_storage_state" in account and not account.get("encrypted_storage_state"))
            ):
                raise ServiceError(
                    "CONFLICT",
                    "Сессия hh.ru недоступна. Вставьте текст описания вакансии.",
                )
            vacancy = _mapping(
                await _await(self.resume_manager.fetch_vacancy_text(user_id, normalized, int(account["id"])))
            )
            description = str(vacancy.get("description") or "").strip()
            if vacancy.get("status") != "SUCCESS" or not description:
                raise ServiceError(
                    "CONFLICT",
                    vacancy.get("message") or "Не удалось загрузить вакансию. Вставьте её текст.",
                )

        try:
            return _mapping(
                await _await(self.ai.match_resume_to_vacancy(str(audit["source_resume_text"]), description))
            )
        except Exception as exc:
            raise ServiceError("AI_UNAVAILABLE", "ИИ-сервис временно недоступен. Повторите попытку позже.") from exc
