"""Shared helpers for background job executors."""

from __future__ import annotations

import logging

from leadscout.notifications import Notification, Notifier


async def deliver_safely(
    notifier: Notifier,
    user_id: int,
    notification: Notification,
    *,
    logger: logging.Logger,
) -> None:
    """Best-effort delivery that never changes the durable job result."""

    try:
        from leadscout.core.task_scope import checkpoint

        await checkpoint()
        await notifier.send(user_id, notification)
    except Exception as exc:
        logger.warning("Notification for user %d failed: %s", user_id, type(exc).__name__)
