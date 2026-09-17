"""Validated question and answer models used by application forms."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

OptionValue = Annotated[str, Field(min_length=1, max_length=500)]


class QuestionField(BaseModel):
    field_id: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=1000)
    answer_type: Literal["text", "textarea", "select", "radio", "checkbox", "unsupported"] = "text"
    required: bool = True
    options: list[OptionValue] = Field(default_factory=list, max_length=30)


class FormAnswer(BaseModel):
    field_id: str = Field(min_length=1, max_length=200)
    answer_type: Literal["text", "textarea", "select", "radio", "checkbox", "unsupported"]
    value: str | list[OptionValue]

    @model_validator(mode="after")
    def validate_value(self):
        if isinstance(self.value, str):
            if not self.value.strip() or len(self.value) > 2000:
                raise ValueError("Answer value is empty or too long")
        elif self.answer_type != "checkbox" or not self.value or len(self.value) > 30:
            raise ValueError("Only checkbox answers may contain multiple values")
        return self


class JobApplicationPayload(BaseModel):
    is_relevant: bool
    relevance_reason: str = Field(max_length=1000)
    cover_letter: str = Field(max_length=5000, description="Письмо на русском языке, 40-70 слов")
    answers: list[FormAnswer] = Field(default_factory=list, max_length=50)
    can_auto_submit: bool
    confidence_score: float = Field(ge=0.0, le=1.0)


class QuestionnairePayload(BaseModel):
    questions: list[QuestionField] = Field(default_factory=list, max_length=50)
    answers: list[FormAnswer] = Field(default_factory=list, max_length=50)
    confidence_score: float = Field(ge=0.0, le=1.0)
    can_auto_submit: bool = False
