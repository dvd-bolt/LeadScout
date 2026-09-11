"""Validated structured-resume and search models."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

KeywordValue = Annotated[str, Field(min_length=1, max_length=200)]


class SearchKeywordsPayload(BaseModel):
    keywords: list[KeywordValue] = Field(default_factory=list, max_length=6)


class WorkExperienceItem(BaseModel):
    company: str = Field(default="", max_length=300)
    position: str = Field(default="", max_length=300)
    city: str = Field(default="", max_length=200)
    start_month: str = Field(default="", max_length=30)
    start_year: str = Field(default="", max_length=4)
    is_current: bool = False
    end_month: str | None = Field(default=None, max_length=30)
    end_year: str | None = Field(default=None, max_length=4)
    description: str = Field(default="", max_length=5000)


class EducationItem(BaseModel):
    level: str = Field(default="", max_length=200)
    institution: str = Field(default="", max_length=500)
    faculty: str = Field(default="", max_length=500)
    specialization: str = Field(default="", max_length=500)
    end_year: str = Field(default="", max_length=4)


class StructuredResume(BaseModel):
    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)
    middle_name: str = Field(default="", max_length=100)
    birth_date: str = Field(default="", max_length=10, description="Дата YYYY-MM-DD или пустая строка")
    title: str = Field(default="", max_length=300)
    salary: int | None = Field(default=None, ge=0, le=100_000_000)
    city: str = Field(default="", max_length=200)
    experiences: list[WorkExperienceItem] = Field(default_factory=list, max_length=30)
    education: list[EducationItem] = Field(default_factory=list, max_length=20)
    skills: list[KeywordValue] = Field(default_factory=list, max_length=50)
    about: str = Field(default="", max_length=5000)


FullStructuredResume = StructuredResume
