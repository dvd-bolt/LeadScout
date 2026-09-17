"""Explicit dependencies consumed by search and questionnaire jobs."""

import inspect
from dataclasses import dataclass
from typing import Any

DB_METHODS = frozenset(
    {
        "get_account_for_user",
        "get_user_accounts",
        "update_account_settings_for_user",
        "claim_pending_questionnaire",
        "get_pending_questionnaire_for_user",
        "finish_pending_questionnaire",
        "get_active_resume_snapshot",
        "is_account_already_applied",
        "has_unresolved_application_attempt",
        "has_open_questionnaire_for_vacancy",
        "record_application_event",
        "record_search_run",
        "record_successful_application",
        "create_application_attempt",
        "update_application_attempt",
        "get_application_attempt",
        "save_pending_questionnaire_account",
        "update_account_session",
        "set_account_pending_captcha",
        "clear_account_pending_captcha",
    }
)


@dataclass
class RuntimeJobDependencies:
    db: Any
    ai: Any
    browser_pool: Any
    applications: Any
    security_factory: Any
    app_url: str = ""

    def __getattr__(self, name):
        if name in DB_METHODS:
            return getattr(self.db, name)
        raise AttributeError(name)

    async def apply_to_hh_vacancy(self, *args, **kwargs):
        return await self.applications.apply_to_hh_vacancy(*args, **kwargs)

    async def submit_approved_questionnaire(self, *args, **kwargs):
        return await self.applications.submit_approved_questionnaire(*args, **kwargs)

    def supports_application_trace(self, operation: str) -> bool:
        """Inspect the actual injected integration, never retry an external form call."""
        callback = getattr(self.applications, operation, None)
        if callback is None:
            return False
        try:
            parameters = inspect.signature(callback).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(parameter.name == "trace" or parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)

    def supports_application_parameter(self, operation: str, name: str) -> bool:
        callback = getattr(self.applications, operation, None)
        if callback is None:
            return False
        try:
            parameters = inspect.signature(callback).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(parameter.name == name or parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters)

    async def extract_search_keywords_from_resume(self, *args, **kwargs):
        return await self.ai.extract_search_keywords_from_resume(*args, **kwargs)

    async def get_browser_engine(self, proxy_url=None):
        return await self.browser_pool.get_engine(proxy_url)

    def session_security(self):
        return self.security_factory()
