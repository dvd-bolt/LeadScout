"""Structured, privacy-safe diagnostics for one application attempt.

The JSONL file intentionally contains identifiers and controlled status codes only.
It must never contain cookies, proxy values, resume text, cover letters, form answers,
URLs with query parameters, or raw exception messages.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from leadscout.core.paths import PROJECT_ROOT

STAGES = frozenset({"SEARCH", "LOADING", "PARSING", "AI_PREPARATION", "FILLING", "SUBMITTING", "CONFIRMING"})

_SAFE_REASONS = {
    "SKIPPED_STOPPED": "Автоматизация остановлена до отправки отклика.",
    "SKIPPED_NO_RESUME": "Не выбрано резюме для отклика.",
    "SKIPPED_NOT_AUTHORIZED": "Для аккаунта требуется повторный вход.",
    "SKIPPED_NO_SESSION": "Сессия аккаунта недоступна; требуется повторный вход.",
    "SKIPPED_LIMIT": "Дневной лимит откликов исчерпан.",
    "SKIPPED_STOP_WORD": "Вакансия пропущена по заданному правилу.",
    "SKIPPED_IRRELEVANT": "Вакансия не прошла проверку соответствия.",
    "SKIPPED_EXTERNAL": "Отклик ведёт на внешний сайт и не отправлялся.",
    "SKIPPED_ALREADY_APPLIED": "Отклик уже был учтён для этого аккаунта.",
    "SKIPPED_NEEDS_REVIEW": "Предыдущий результат не подтверждён; повторная отправка отключена до проверки hh.ru.",
    "ERROR_SESSION_EXPIRED": "Сессия истекла или требует повторного входа.",
    "ERROR_CAPTCHA": "hh.ru запросил CAPTCHA; требуется действие пользователя.",
    "ERROR_INVALID_URL": "Ссылка вакансии hh.ru указана некорректно.",
    "ERROR_NO_RESUME": "Выбранное резюме не найдено в форме отклика.",
    "ERROR_UNAVAILABLE": "Вакансия недоступна или закрыта.",
    "ERROR_EXTERNAL": "Страница вакансии перенаправила на внешний сайт.",
    "ERROR_INCOMPLETE": "Страница вакансии не завершила загрузку; отклик не отправлялся.",
    "ERROR_AI": "Не удалось подготовить отклик с помощью ИИ.",
    "ERROR_TIMEOUT": "Операция превысила время ожидания; отправка не подтверждена.",
    "ERROR_SUBMIT_UNCONFIRMED": "Отправка не подтверждена hh.ru; повтор не выполнялся.",
    "ERROR_LOCAL_PERSISTENCE": "hh.ru подтвердил отклик, но локальная запись требует проверки.",
    "ERROR_BROWSER": "Внешняя форма не завершила операцию; точная причина не установлена.",
    "ERROR_NO_BUTTON": "Форма отклика недоступна для этой вакансии.",
    "ERROR_RESUME_SELECTION": "Выбранное резюме не найдено в форме отклика.",
    "ERROR_LETTER_FIELD": "Не удалось подготовить обязательное поле формы.",
    "ERROR_FORM": "Не удалось заполнить все обязательные поля анкеты.",
    "ERROR_SUBMIT_BUTTON": "Кнопка отправки формы недоступна.",
    "QUESTIONNAIRE_REQUIRED": "Нужна проверка и подтверждение анкеты.",
    "ALREADY_APPLIED": "Отклик уже подтверждён на hh.ru.",
    "APPLIED_DIRECT": "Отправка подтверждена hh.ru.",
    "APPLIED_WITH_LETTER": "Отправка подтверждена hh.ru.",
    "APPLIED_WITH_QUESTIONNAIRE": "Отправка анкеты подтверждена hh.ru.",
    "APPLIED_CONFIRMED_MANUALLY": "Пользователь подтвердил, что отклик появился на hh.ru.",
    "REVIEWED_NOT_APPLIED": "Пользователь подтвердил, что отклик не появился на hh.ru; повторная отправка разрешена.",
}


def safe_reason_for_status(status: str) -> str:
    """Map only observed outcome codes to user-safe text, never exception text."""
    return _SAFE_REASONS.get(status, "Точная причина не установлена.")


def _vacancy_id(value: str) -> str:
    match = re.search(r"(?:^|/vacancy/)(\d+)(?:/|$|[?#])", str(value or ""))
    return match.group(1) if match else str(value or "")[:200]


def _diagnostic_logger() -> logging.Logger:
    logger = logging.getLogger("leadscout.application_attempts")
    if logger.handlers:
        return logger
    log_dir = Path(PROJECT_ROOT) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "application_attempts.jsonl", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _write_local_event(attempt_id: str, user_id: int, account_id: int, vacancy_hh_id: str, stage: str, outcome: str) -> None:
    """Best-effort audit write. Deliberately omit any text originating outside our code."""
    try:
        _diagnostic_logger().info(
            json.dumps(
                {
                    "attempt_id": attempt_id[:64],
                    "user_id": user_id,
                    "account_id": account_id,
                    "vacancy_hh_id": vacancy_hh_id[:200],
                    "stage": stage[:64],
                    "outcome": outcome[:64],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    except Exception:
        # Diagnostics must not turn a known integration result into a different failure.
        logging.getLogger(__name__).warning("Could not write application diagnostic event")


@dataclass(slots=True)
class ApplicationAttemptTracer:
    """Owner-scoped tracker injected into the browser boundary for one attempt."""

    dependencies: Any
    attempt_id: str
    user_id: int
    account_id: int
    vacancy_hh_id: str
    current_stage: str = "SEARCH"

    def __post_init__(self) -> None:
        self.vacancy_hh_id = _vacancy_id(self.vacancy_hh_id)

    @classmethod
    async def start(
        cls, dependencies: Any, user_id: int, account_id: int, vacancy_hh_id: str, vacancy_title: str = ""
    ) -> "ApplicationAttemptTracer | None":
        try:
            attempt_id = await dependencies.create_application_attempt(
                user_id, account_id, vacancy_hh_id, vacancy_title
            )
        except Exception:
            logging.getLogger(__name__).warning("Could not create application diagnostic record")
            return None
        if not attempt_id:
            return None
        tracer = cls(dependencies, attempt_id, user_id, account_id, vacancy_hh_id)
        await tracer.stage("SEARCH")
        return tracer

    async def stage(self, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"Unknown application stage: {stage}")
        self.current_stage = stage
        _write_local_event(self.attempt_id, self.user_id, self.account_id, self.vacancy_hh_id, stage, "IN_PROGRESS")
        try:
            await self.dependencies.update_application_attempt(
                self.attempt_id, self.user_id, self.account_id, stage
            )
        except Exception:
            logging.getLogger(__name__).warning("Could not update application diagnostic record")

    async def finish(self, status: str, stage: str | None = None) -> str:
        terminal_stage = stage or self.current_stage
        if terminal_stage not in STAGES:
            raise ValueError(f"Unknown application stage: {terminal_stage}")
        self.current_stage = terminal_stage
        reason = safe_reason_for_status(status)
        _write_local_event(
            self.attempt_id,
            self.user_id,
            self.account_id,
            self.vacancy_hh_id,
            terminal_stage,
            status,
        )
        try:
            await self.dependencies.update_application_attempt(
                self.attempt_id,
                self.user_id,
                self.account_id,
                terminal_stage,
                outcome=status,
                safe_reason=reason,
            )
        except Exception:
            logging.getLogger(__name__).warning("Could not finish application diagnostic record")
        return reason
