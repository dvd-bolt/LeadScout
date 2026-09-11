"""Notification contracts used by background jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Notification:
    """A transport-neutral notification ready for delivery."""

    text: str
    parse_mode: str | None = "HTML"
    reply_markup: Any | None = None
    options: dict[str, Any] = field(default_factory=dict)


class Notifier(Protocol):
    """Minimal async delivery interface implemented by every transport."""

    async def send(self, user_id: int, notification: Notification) -> None: ...


class NullNotifier:
    """Default notifier for scheduler-only and test processes."""

    async def send(self, user_id: int, notification: Notification) -> None:
        return None


class RecordingNotifier:
    """Small test notifier that records messages without external effects."""

    def __init__(self) -> None:
        self.messages: list[tuple[int, Notification]] = []

    async def send(self, user_id: int, notification: Notification) -> None:
        self.messages.append((user_id, notification))


# A descriptive alias for consumers that prefer an explicitly test-oriented name.
TestNotifier = RecordingNotifier
