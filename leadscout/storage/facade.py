"""A bound repository facade owned by one application context."""

from functools import partial

from .connection import Database
from .migrations import init_db
from .repositories import accounts, applications, audits, operations, questionnaires, resumes, users

REPOSITORY_FUNCTIONS = {
    "get_or_create_user": users.get_or_create_user,
    "update_user_session": users.update_user_session,
    "get_user_session": users.get_user_session,
    "update_user_settings": users.update_user_settings,
    "get_user_accounts": accounts.get_user_accounts,
    "get_enabled_accounts": accounts.get_enabled_accounts,
    "set_next_scheduled_search_at": accounts.set_next_scheduled_search_at,
    "get_account_for_user": accounts.get_account_for_user,
    "get_account_by_login": accounts.get_account_by_login,
    "get_active_account": accounts.get_active_account,
    "set_active_account": accounts.set_active_account,
    "create_hh_account": accounts.create_hh_account,
    "_update_account": accounts.update_account,
    "update_account_settings_for_user": accounts.update_account_settings_for_user,
    "update_account_session": accounts.update_account_session,
    "delete_hh_account_for_user": accounts.delete_hh_account_for_user,
    "reset_all_account_daily_limits": accounts.reset_all_account_daily_limits,
    "is_account_already_applied": applications.is_account_already_applied,
    "record_application_event": applications.record_application_event,
    "record_successful_application": applications.record_successful_application,
    "is_already_applied": applications.is_already_applied,
    "get_user_recent_applies": applications.get_user_recent_applies,
    "get_application_stats": applications.get_application_stats,
    "list_application_events": applications.list_application_events,
    "list_pending_questionnaires": questionnaires.list_pending_questionnaires,
    "count_pending_reviews": questionnaires.count_pending_reviews,
    "recover_interrupted_questionnaires": questionnaires.recover_interrupted_questionnaires,
    "save_pending_questionnaire_account": questionnaires.save_pending_questionnaire_account,
    "save_pending_questionnaire": questionnaires.save_pending_questionnaire,
    "get_pending_questionnaire_for_user": questionnaires.get_pending_questionnaire_for_user,
    "claim_pending_questionnaire": questionnaires.claim_pending_questionnaire,
    "skip_pending_questionnaire": questionnaires.skip_pending_questionnaire,
    "finish_pending_questionnaire": questionnaires.finish_pending_questionnaire,
    "update_pending_questionnaire_status": questionnaires.update_pending_questionnaire_status,
    "update_pending_questionnaire_letter": questionnaires.update_pending_questionnaire_letter,
    "update_pending_questionnaire_answers": questionnaires.update_pending_questionnaire_answers,
    "edit_pending_questionnaire": questionnaires.edit_pending_questionnaire,
    "sync_resume_snapshots": resumes.sync_resume_snapshots,
    "list_resume_snapshots": resumes.list_resume_snapshots,
    "get_resume_snapshot_for_user": resumes.get_resume_snapshot_for_user,
    "get_resume_snapshot_by_hh_id": resumes.get_resume_snapshot_by_hh_id,
    "get_active_resume_snapshot": resumes.get_active_resume_snapshot,
    "attach_resume_text": resumes.attach_resume_text,
    "set_active_resume_snapshot": resumes.set_active_resume_snapshot,
    "delete_resume_snapshot": resumes.delete_resume_snapshot,
    "get_user_resumes_json": resumes.get_user_resumes_json,
    "save_resume_audit": audits.save_resume_audit,
    "get_user_latest_audit": audits.get_user_latest_audit,
    "list_resume_audits": audits.list_resume_audits,
    "get_resume_audit_for_user": audits.get_resume_audit_for_user,
    "create_operation": operations.create_operation,
    "recover_interrupted_operations": operations.recover_interrupted_operations,
    "complete_operation": operations.complete_operation,
    "start_operation": operations.start_operation,
    "set_operation_needs_input": operations.set_operation_needs_input,
    "get_operation_for_user": operations.get_operation_for_user,
}


class Storage:
    def __init__(self, database: Database):
        self.database = database

    async def init_db(self):
        await init_db(self.database)

    def get_db_connection(self):
        return self.database.connection()

    def __getattr__(self, name):
        if name not in REPOSITORY_FUNCTIONS:
            raise AttributeError(name)
        method = partial(REPOSITORY_FUNCTIONS[name], self.database)
        setattr(self, name, method)
        return method
