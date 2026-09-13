"""ResumeService business workflow."""

from __future__ import annotations

from typing import Any

from leadscout.documents.pdf_reader import PDFValidationError

from .common import _await, _mapping, _Service
from .errors import ServiceError


class ResumeService(_Service):
    def __init__(self, *, db: Any, coordinator: Any, resume_manager: Any):
        super().__init__(db=db, coordinator=coordinator)
        self.resume_manager = resume_manager

    async def sync(self, user_id: int, account_id: int) -> dict:
        await self._account(user_id, account_id)
        return _mapping(await _await(self.resume_manager.fetch_user_resumes(user_id, account_id)))

    async def import_pdf(
        self,
        user_id: int,
        account_id: int,
        path: str,
        structured: dict | Any | None = None,
    ) -> dict:
        await self._account(user_id, account_id)
        try:
            result = _mapping(
                await _await(
                    self.resume_manager.upload_pdf_resume_to_hh(
                        user_id,
                        str(path),
                        account_id=account_id,
                        structured_override=structured,
                    )
                )
            )
        except PDFValidationError as exc:
            return {"status": "ERROR", "message": str(exc)}
        # NEEDS_FIELDS is deliberately preserved for an HTTP operation adapter.
        if result.get("status") == "NEEDS_FIELDS":
            result.setdefault("missing_fields", [])
            result.setdefault("structured", {})
        return result

    async def delete(self, user_id: int, account_id: int, snapshot_id: int) -> dict:
        await self._account(user_id, account_id)
        snapshot = await _await(self.db.get_resume_snapshot_for_user(user_id, snapshot_id))
        if not snapshot or int(snapshot.get("account_id", -1)) != int(account_id):
            raise ServiceError("NOT_FOUND", "Резюме не найдено.")
        return _mapping(
            await _await(self.resume_manager.delete_resume_on_hh(user_id, snapshot["hh_resume_id"], account_id))
        )
