"""Validated request schemas for the Mini App API."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class TelegramLogin(BaseModel):
    init_data: str = Field(min_length=20, max_length=10_000)


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_name: str | None = Field(default=None, max_length=120)
    keywords: str | None = Field(default=None, max_length=1_000)
    stop_words: str | None = Field(default=None, max_length=1_000)
    min_salary: int | None = Field(default=None, ge=0, le=100_000_000)
    daily_limit: int | None = Field(default=None, ge=1, le=200)
    only_remote: bool | None = None
    send_cover_letter: bool | None = None
    proxy_url: str | None = Field(default=None, max_length=2_000)


class LoginStart(BaseModel):
    phone_or_email: str = Field(min_length=5, max_length=254)
    account_name: str = Field(default="", max_length=120)


class OtpSubmission(BaseModel):
    code: str = Field(pattern=r"^\d{4,8}$")
    account_id: int | None = Field(default=None, gt=0)


class CaptchaSubmission(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    account_id: int | None = Field(default=None, gt=0)


class LoginFlowAccount(BaseModel):
    account_id: int | None = Field(default=None, gt=0)


class ConfirmBody(BaseModel):
    confirm: bool


class FormAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str = Field(min_length=1, max_length=256)
    answer_type: str = Field(min_length=1, max_length=32)
    value: str | list[str]

    @model_validator(mode="after")
    def valid_value(self):
        values = self.value if isinstance(self.value, list) else [self.value]
        if not values or len(values) > 30 or any(not value.strip() or len(value) > 2_000 for value in values):
            raise ValueError("Некорректный ответ анкеты")
        if isinstance(self.value, list) and self.answer_type != "checkbox":
            raise ValueError("Несколько значений допустимы только для флажков")
        return self


class QuestionnaireUpdate(BaseModel):
    expected_revision: int = Field(ge=0)
    cover_letter: str | None = Field(default=None, max_length=10_000)
    answers: list[FormAnswer] | None = Field(default=None, max_length=50)


class QuestionnaireConfirm(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)


class ApplicationAttemptResolution(BaseModel):
    applied: bool


class AuditRequest(BaseModel):
    account_id: int | None = Field(default=None, gt=0)
    resume_snapshot_id: int | None = Field(default=None, gt=0)
    resume_text: str | None = Field(default=None, min_length=50, max_length=50_000)


class MatchRequest(BaseModel):
    vacancy_text: str | None = Field(default=None, min_length=15, max_length=50_000)
    vacancy_url: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def exactly_one_source(self):
        if bool(self.vacancy_text and self.vacancy_text.strip()) == bool(self.vacancy_url and self.vacancy_url.strip()):
            raise ValueError("Укажите ровно один источник вакансии: текст или ссылку")
        return self
