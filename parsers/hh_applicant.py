"""Legacy application entrypoints and stateless form helpers."""

from leadscout.integrations.application_forms import (
    _is_hh_location,
    _is_visible,
    _open_letter_and_fill,
    _question_index,
    _submit_response_form,
    effective_cover_letter,
    extract_questionnaire_fields,
    fill_questionnaire_form,
    handle_resume_selection_if_needed,
    questionnaire_requires_confirmation,
    submit_approved_questionnaire,
    verify_hh_application_success,
)
from leadscout.integrations.vacancies import extract_vacancy_details
from leadscout.runtime.context import get_default_context


async def apply_to_hh_vacancy(*args, **kwargs):
    return await get_default_context().applications.apply_to_hh_vacancy(*args, **kwargs)


__all__ = [
    "extract_vacancy_details",
    "_is_visible",
    "handle_resume_selection_if_needed",
    "extract_questionnaire_fields",
    "_question_index",
    "fill_questionnaire_form",
    "questionnaire_requires_confirmation",
    "effective_cover_letter",
    "verify_hh_application_success",
    "_open_letter_and_fill",
    "_submit_response_form",
    "_is_hh_location",
    "submit_approved_questionnaire",
    "apply_to_hh_vacancy",
]
