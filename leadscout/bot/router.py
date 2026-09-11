"""Bot router assembly."""

from __future__ import annotations

from aiogram import Router

from leadscout.core.config import OWNER_TELEGRAM_IDS

from .handlers import create_handlers_router
from .legacy_callbacks import create_legacy_router


def create_router(owner_id: int | None = None, *, owner_ids: tuple[int, ...] | None = None) -> Router:
    if owner_ids is None:
        owner_ids = (owner_id,) if owner_id else OWNER_TELEGRAM_IDS
    root = Router(name="leadscout")
    root.include_router(create_handlers_router(owner_ids=owner_ids))
    root.include_router(create_legacy_router(owner_ids=owner_ids))
    return root


router = create_router()

__all__ = ["create_router", "router"]
