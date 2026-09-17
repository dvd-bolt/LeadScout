"""Shared contracts for background-operation results and lifecycle mapping."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal, NotRequired, TypedDict

OperationResultStatus = Literal[
    "SUCCESS", "SUCCEEDED", "STARTED", "ALREADY_RUNNING", "READY", "CANCELLED", "STOPPED",
    "NEEDS_FIELDS", "NEEDS_INPUT", "NEEDS_REVIEW", "NEEDS_ACTION", "PARTIAL", "UNCERTAIN",
    "WAITING_FOR_CAPTCHA", "WAITING_FOR_OTP", "WAITING_FOR_CODE", "WAITING_FOR_SMS",
    "INVALID_CAPTCHA", "INVALID_CODE",
    "PENDING", "SUBMITTED", "SKIPPED", "SKIPPED_LIMIT", "SKIPPED_NOT_AUTHORIZED",
    "SKIPPED_NO_RESUME", "SKIPPED_NO_SESSION", "SKIPPED_STOPPED",
    "CLIENT_UPDATE_REQUIRED", "DAILY_LIMIT", "ERROR", "ERROR_CAPTCHA", "ERROR_INVALID_URL",
    "ERROR_SESSION_EXPIRED", "EXPIRED_SESSION", "FAILED", "MISSING_RESUME", "NOT_FOUND", "NO_SESSION",
]


class OperationResult(TypedDict):
    status: OperationResultStatus
    message: NotRequired[str]
    code: NotRequired[str]


class ResultDisposition(StrEnum):
    SUCCESS = "SUCCESS"
    NEEDS_INPUT = "NEEDS_INPUT"
    FAILED = "FAILED"


NEEDS_INPUT_RESULTS = frozenset({
    "NEEDS_FIELDS", "NEEDS_INPUT", "NEEDS_REVIEW", "NEEDS_ACTION", "PARTIAL", "UNCERTAIN",
    "WAITING_FOR_CAPTCHA", "WAITING_FOR_OTP", "WAITING_FOR_CODE", "WAITING_FOR_SMS",
    "INVALID_CAPTCHA", "INVALID_CODE",
})
SUCCESS_RESULTS = frozenset({"SUCCESS", "SUCCEEDED", "STARTED", "ALREADY_RUNNING", "CANCELLED", "READY"})


def result_disposition(status: str) -> ResultDisposition:
    if status in NEEDS_INPUT_RESULTS:
        return ResultDisposition.NEEDS_INPUT
    if status in SUCCESS_RESULTS or status.startswith("SKIPPED"):
        return ResultDisposition.SUCCESS
    return ResultDisposition.FAILED


__all__ = [
    "OperationResult", "OperationResultStatus", "ResultDisposition", "result_disposition"
]
