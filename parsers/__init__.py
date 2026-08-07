"""
LeadScout AI — Пакет модулей автоотклика hh.ru (Patchright Stealth).
"""

from parsers.hh_browser import HHBrowserEngine, SharedBrowserPool, intercept_network_traffic
from parsers.hh_login import HHLoginSession, HHLoginManager
from parsers.hh_applicant import apply_to_hh_vacancy, submit_approved_questionnaire, extract_vacancy_details

__all__ = [
    "HHBrowserEngine",
    "SharedBrowserPool",
    "intercept_network_traffic",
    "HHLoginSession",
    "HHLoginManager",
    "apply_to_hh_vacancy",
    "submit_approved_questionnaire",
    "extract_vacancy_details",
]

