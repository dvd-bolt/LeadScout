"""Temporary compatibility facade for the package-based storage layer."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from inspect import signature
from typing import Any

import aiosqlite

from leadscout.core.config import DB_PATH
from leadscout.runtime.context import get_default_context
from leadscout.storage.connection import Database
from leadscout.storage.migrations import (
    SCHEMA_VERSION,
    add_missing_column,
    table_columns,
)
from leadscout.storage.migrations import init_db as initialize_database
from leadscout.storage.repositories import (
    accounts,
    applications,
    audits,
    operations,
    questionnaires,
    resumes,
    users,
)

_CONFIGURED_DB_PATH = DB_PATH
DEFAULT_DATABASE = get_default_context().db.database
_INITIAL_DATABASE = DEFAULT_DATABASE


def get_default_database() -> Database:
    """Return the injected database, while honoring a patched legacy DB_PATH."""
    if DB_PATH != _CONFIGURED_DB_PATH:
        return Database(DB_PATH, timeout=DEFAULT_DATABASE.timeout)
    if DEFAULT_DATABASE is not _INITIAL_DATABASE:
        return DEFAULT_DATABASE
    return get_default_context().db.database


def get_db_connection():
    """Compatibility alias for ``Database.connection()``."""
    return get_default_database().connection()


async def init_db() -> None:
    await initialize_database(get_default_database())


def _using_default(function: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(function)
    async def compatibility_wrapper(*args, **kwargs):
        return await function(get_default_database(), *args, **kwargs)

    parameters = tuple(signature(function).parameters.values())[1:]
    compatibility_wrapper.__signature__ = signature(function).replace(parameters=parameters)
    return compatibility_wrapper


async def _table_columns(connection: aiosqlite.Connection, table: str) -> set[str]:
    return await table_columns(connection, table)


async def _add_missing_column(connection: aiosqlite.Connection, table: str, definition: str) -> None:
    await add_missing_column(connection, table, definition)


AccountLimitError = accounts.AccountLimitError
DuplicateAccountError = accounts.DuplicateAccountError
ACCOUNT_SETTINGS = accounts.ACCOUNT_SETTINGS
calculate_text_hash = operations.calculate_text_hash
_today = accounts.today
_normalize_login = accounts.normalize_login
_normalize_vacancy_hh_id = applications.normalize_vacancy_hh_id
_decode_audit = audits.decode_audit

get_or_create_user = _using_default(users.get_or_create_user)
update_user_session = _using_default(users.update_user_session)
get_user_session = _using_default(users.get_user_session)
update_user_settings = _using_default(users.update_user_settings)
get_user_accounts = _using_default(accounts.get_user_accounts)
get_enabled_accounts = _using_default(accounts.get_enabled_accounts)
set_next_scheduled_search_at = _using_default(accounts.set_next_scheduled_search_at)
get_account_for_user = _using_default(accounts.get_account_for_user)
get_account_by_login = _using_default(accounts.get_account_by_login)
get_active_account = _using_default(accounts.get_active_account)
set_active_account = _using_default(accounts.set_active_account)
create_hh_account = _using_default(accounts.create_hh_account)
_update_account = _using_default(accounts.update_account)
update_account_settings_for_user = _using_default(accounts.update_account_settings_for_user)
update_account_session = _using_default(accounts.update_account_session)
set_account_pending_captcha = _using_default(accounts.set_account_pending_captcha)
clear_account_pending_captcha = _using_default(accounts.clear_account_pending_captcha)
delete_hh_account_for_user = _using_default(accounts.delete_hh_account_for_user)
reset_all_account_daily_limits = _using_default(accounts.reset_all_account_daily_limits)

is_account_already_applied = _using_default(applications.is_account_already_applied)
has_unresolved_application_attempt = _using_default(applications.has_unresolved_application_attempt)
record_application_event = _using_default(applications.record_application_event)
record_successful_application = _using_default(applications.record_successful_application)
create_application_attempt = _using_default(applications.create_application_attempt)
update_application_attempt = _using_default(applications.update_application_attempt)
get_application_attempt = _using_default(applications.get_application_attempt)
resolve_application_attempt = _using_default(applications.resolve_application_attempt)
is_already_applied = _using_default(applications.is_already_applied)
get_user_recent_applies = _using_default(applications.get_user_recent_applies)
get_application_stats = _using_default(applications.get_application_stats)
list_application_events = _using_default(applications.list_application_events)

list_pending_questionnaires = _using_default(questionnaires.list_pending_questionnaires)
recover_interrupted_questionnaires = _using_default(questionnaires.recover_interrupted_questionnaires)
save_pending_questionnaire_account = _using_default(questionnaires.save_pending_questionnaire_account)
save_pending_questionnaire = _using_default(questionnaires.save_pending_questionnaire)
get_pending_questionnaire_for_user = _using_default(questionnaires.get_pending_questionnaire_for_user)
claim_pending_questionnaire = _using_default(questionnaires.claim_pending_questionnaire)
skip_pending_questionnaire = _using_default(questionnaires.skip_pending_questionnaire)
finish_pending_questionnaire = _using_default(questionnaires.finish_pending_questionnaire)
update_pending_questionnaire_status = _using_default(questionnaires.update_pending_questionnaire_status)
update_pending_questionnaire_letter = _using_default(questionnaires.update_pending_questionnaire_letter)
update_pending_questionnaire_answers = _using_default(questionnaires.update_pending_questionnaire_answers)
edit_pending_questionnaire = _using_default(questionnaires.edit_pending_questionnaire)

sync_resume_snapshots = _using_default(resumes.sync_resume_snapshots)
list_resume_snapshots = _using_default(resumes.list_resume_snapshots)
get_resume_snapshot_for_user = _using_default(resumes.get_resume_snapshot_for_user)
get_resume_snapshot_by_hh_id = _using_default(resumes.get_resume_snapshot_by_hh_id)
get_active_resume_snapshot = _using_default(resumes.get_active_resume_snapshot)
attach_resume_text = _using_default(resumes.attach_resume_text)
set_active_resume_snapshot = _using_default(resumes.set_active_resume_snapshot)
delete_resume_snapshot = _using_default(resumes.delete_resume_snapshot)
get_user_resumes_json = _using_default(resumes.get_user_resumes_json)
save_resume_audit = _using_default(audits.save_resume_audit)
get_user_latest_audit = _using_default(audits.get_user_latest_audit)
list_resume_audits = _using_default(audits.list_resume_audits)
get_resume_audit_for_user = _using_default(audits.get_resume_audit_for_user)

create_operation = _using_default(operations.create_operation)
recover_interrupted_operations = _using_default(operations.recover_interrupted_operations)
complete_operation = _using_default(operations.complete_operation)
start_operation = _using_default(operations.start_operation)
set_operation_needs_input = _using_default(operations.set_operation_needs_input)
get_operation_for_user = _using_default(operations.get_operation_for_user)

__all__ = [
    "ACCOUNT_SETTINGS",
    "AccountLimitError",
    "DEFAULT_DATABASE",
    "DB_PATH",
    "Database",
    "DuplicateAccountError",
    "SCHEMA_VERSION",
    "attach_resume_text",
    "calculate_text_hash",
    "claim_pending_questionnaire",
    "complete_operation",
    "create_application_attempt",
    "create_hh_account",
    "create_operation",
    "delete_hh_account_for_user",
    "delete_resume_snapshot",
    "edit_pending_questionnaire",
    "finish_pending_questionnaire",
    "get_account_by_login",
    "get_account_for_user",
    "get_active_account",
    "get_application_attempt",
    "has_unresolved_application_attempt",
    "get_active_resume_snapshot",
    "get_application_stats",
    "get_db_connection",
    "get_default_database",
    "get_operation_for_user",
    "get_or_create_user",
    "get_pending_questionnaire_for_user",
    "get_resume_audit_for_user",
    "get_resume_snapshot_by_hh_id",
    "get_resume_snapshot_for_user",
    "get_user_accounts",
    "get_user_latest_audit",
    "get_user_recent_applies",
    "get_user_resumes_json",
    "get_user_session",
    "init_db",
    "is_account_already_applied",
    "is_already_applied",
    "list_application_events",
    "list_pending_questionnaires",
    "list_resume_audits",
    "list_resume_snapshots",
    "record_application_event",
    "record_successful_application",
    "resolve_application_attempt",
    "recover_interrupted_operations",
    "recover_interrupted_questionnaires",
    "reset_all_account_daily_limits",
    "save_pending_questionnaire",
    "save_pending_questionnaire_account",
    "save_resume_audit",
    "set_account_pending_captcha",
    "clear_account_pending_captcha",
    "set_operation_needs_input",
    "set_active_account",
    "set_active_resume_snapshot",
    "set_next_scheduled_search_at",
    "start_operation",
    "skip_pending_questionnaire",
    "sync_resume_snapshots",
    "update_account_session",
    "update_application_attempt",
    "update_account_settings_for_user",
    "update_pending_questionnaire_answers",
    "update_pending_questionnaire_letter",
    "update_pending_questionnaire_status",
    "update_user_session",
    "update_user_settings",
]
