"""Validated AI result models for resume audits and vacancy matching."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, Field

from leadscout.models.resumes import KeywordValue

ListText = Annotated[str, Field(min_length=1, max_length=1000)]


class CategoryScores(BaseModel):
    hard_skills: int = Field(ge=0, le=100)
    impact_metrics: int = Field(ge=0, le=100)
    parseability: int = Field(ge=0, le=100)
    timeline: int = Field(ge=0, le=100)
    style: int = Field(ge=0, le=100)


class ActionableInsight(BaseModel):
    tier: int = Field(ge=1, le=3)
    title: str = Field(max_length=300)
    description: str = Field(max_length=3000)
    score_impact: str = Field(max_length=100)


class ResumeAuditPayload(BaseModel):
    is_it_profession: bool
    profession_name: str = Field(max_length=300)
    rejection_reason: str = Field(default="", max_length=2000)
    overall_score: int = Field(default=0, ge=0, le=100)
    category_scores: CategoryScores = Field(
        default_factory=lambda: CategoryScores(
            hard_skills=0,
            impact_metrics=0,
            parseability=0,
            timeline=0,
            style=0,
        )
    )
    penalties: list[ListText] = Field(default_factory=list, max_length=20)
    top_recommendations: list[ListText] = Field(default_factory=list, max_length=5)
    insights: list[ActionableInsight] = Field(default_factory=list, max_length=20)
    summary_text: str = Field(default="", max_length=3000)


class VacancyMatchPayload(BaseModel):
    match_score: int = Field(ge=0, le=100)
    is_suitable: bool
    matching_skills: list[KeywordValue] = Field(default_factory=list, max_length=30)
    missing_skills: list[KeywordValue] = Field(default_factory=list, max_length=30)
    advice_for_apply: str = Field(max_length=3000)
