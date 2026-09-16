"""Versioned data contract for locally prepared hh.ru resumes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ProfessionData(DraftModel):
    title: str = Field(default="", max_length=300)
    hh_profession: str = Field(default="", max_length=300)
    hh_profession_id: str = Field(default="", max_length=100)
    specializations: list[str] = Field(default_factory=list, max_length=20)


class PersonalData(DraftModel):
    first_name: str = Field(default="", max_length=100)
    last_name: str = Field(default="", max_length=100)
    middle_name: str = Field(default="", max_length=100)
    birth_date: str = Field(default="", max_length=10)
    gender: str = Field(default="", max_length=30)
    city: str = Field(default="", max_length=200)
    citizenships: list[str] = Field(default_factory=list, max_length=20)
    work_authorizations: list[str] = Field(default_factory=list, max_length=20)


class ContactData(DraftModel):
    phone: str = Field(default="", max_length=100)
    email: str = Field(default="", max_length=320)
    telegram: str = Field(default="", max_length=100)
    preferred: str = Field(default="", max_length=50)
    methods: list[str] = Field(default_factory=list, max_length=10)


class WorkConditionsData(DraftModel):
    salary: int | None = Field(default=None, ge=0, le=100_000_000)
    currency: str = Field(default="RUR", max_length=10)
    employment_types: list[str] = Field(default_factory=list, max_length=10)
    schedules: list[str] = Field(default_factory=list, max_length=10)
    work_formats: list[str] = Field(default_factory=list, max_length=10)
    relocation: str = Field(default="", max_length=100)
    business_trips: str = Field(default="", max_length=100)


class SkillData(DraftModel):
    name: str = Field(default="", max_length=200)
    level: str = Field(default="", max_length=100)


class ExperienceData(DraftModel):
    company: str = Field(default="", max_length=300)
    position: str = Field(default="", max_length=300)
    city: str = Field(default="", max_length=200)
    start_month: str = Field(default="", max_length=30)
    start_year: str = Field(default="", max_length=4)
    is_current: bool = False
    end_month: str = Field(default="", max_length=30)
    end_year: str = Field(default="", max_length=4)
    description: str = Field(default="", max_length=10_000)
    selected: bool = True


class EducationData(DraftModel):
    level: str = Field(default="", max_length=200)
    institution: str = Field(default="", max_length=500)
    faculty: str = Field(default="", max_length=500)
    specialization: str = Field(default="", max_length=500)
    end_year: str = Field(default="", max_length=4)
    selected: bool = True


class LanguageData(DraftModel):
    name: str = Field(default="", max_length=100)
    level: str = Field(default="", max_length=100)


class NamedDetailData(DraftModel):
    name: str = Field(default="", max_length=500)
    organization: str = Field(default="", max_length=500)
    year: str = Field(default="", max_length=4)
    description: str = Field(default="", max_length=2000)


class LinkData(DraftModel):
    label: str = Field(default="", max_length=200)
    url: str = Field(default="", max_length=2000)


class AdditionalData(DraftModel):
    courses: list[NamedDetailData] = Field(default_factory=list, max_length=30)
    exams: list[NamedDetailData] = Field(default_factory=list, max_length=30)
    certificates: list[NamedDetailData] = Field(default_factory=list, max_length=30)
    recommendations: list[NamedDetailData] = Field(default_factory=list, max_length=30)
    driving_licenses: list[str] = Field(default_factory=list, max_length=20)
    has_car: bool = False


class AboutData(DraftModel):
    text: str = Field(default="", max_length=10_000)
    links: list[LinkData] = Field(default_factory=list, max_length=30)


class PublicationData(DraftModel):
    visibility: str = Field(default="", max_length=100)
    target_account_confirmed: bool = False


class ResumeDraftData(DraftModel):
    profession: ProfessionData = Field(default_factory=ProfessionData)
    personal: PersonalData = Field(default_factory=PersonalData)
    contacts: ContactData = Field(default_factory=ContactData)
    work_conditions: WorkConditionsData = Field(default_factory=WorkConditionsData)
    skills: list[SkillData] = Field(default_factory=list, max_length=100)
    experiences: list[ExperienceData] = Field(default_factory=list, max_length=50)
    education: list[EducationData] = Field(default_factory=list, max_length=30)
    languages: list[LanguageData] = Field(default_factory=list, max_length=30)
    additional: AdditionalData = Field(default_factory=AdditionalData)
    about: AboutData = Field(default_factory=AboutData)
    publication: PublicationData = Field(default_factory=PublicationData)

DraftStatus = Literal[
    "DRAFT",
    "PARSING",
    "READY",
    "PUBLISHING",
    "COMPLETED",
    "NEEDS_INPUT",
    "NEEDS_REVIEW",
    "FAILED",
]


def empty_resume_draft() -> dict:
    return ResumeDraftData().model_dump()


__all__ = ["DraftStatus", "ResumeDraftData", "empty_resume_draft"]
