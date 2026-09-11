"""Explicit dependencies consumed by search and questionnaire jobs."""

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
        "record_application_event",
        "record_successful_application",
        "save_pending_questionnaire_account",
        "update_account_session",
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

    async def extract_search_keywords_from_resume(self, *args, **kwargs):
        return await self.ai.extract_search_keywords_from_resume(*args, **kwargs)

    async def get_browser_engine(self, proxy_url=None):
        return await self.browser_pool.get_engine(proxy_url)

    def session_security(self):
        return self.security_factory()
