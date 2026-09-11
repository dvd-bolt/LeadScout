"""Public response adapters that remove private storage fields."""

from __future__ import annotations

import json

from leadscout.runtime.scheduler import next_search_time


def public_account(account: dict, coordinator, scheduler=None) -> dict:
    session_status = str(account.get("session_status") or "")
    if session_status in {"ERROR", "FAILED"}:
        automation_state = "ERROR"
    elif session_status != "ACTIVE":
        automation_state = "NEEDS_LOGIN"
    elif coordinator.is_running(int(account.get("user_id", 0)), int(account.get("id", 0))):
        automation_state = "SEARCHING"
    elif account.get("auto_apply_enabled"):
        automation_state = "QUEUED"
    else:
        automation_state = "WAITING"
    enabled = bool(account.get("auto_apply_enabled"))
    return {
        "id": account["id"],
        "account_name": account.get("account_name") or account.get("phone_or_email"),
        "session_status": account.get("session_status"),
        "active_resume_hh_id": account.get("active_resume_hh_id", ""),
        "active_resume_title": account.get("active_resume_title", ""),
        "daily_limit": account.get("daily_limit", 50),
        "applied_today": account.get("applied_today", 0),
        "applied_date": account.get("applied_date", ""),
        "auto_apply_enabled": enabled,
        "automation_state": automation_state,
        "only_remote": bool(account.get("only_remote")),
        "send_cover_letter": bool(account.get("send_cover_letter")),
        "min_salary": account.get("min_salary", 0),
        "keywords": account.get("keywords", ""),
        "stop_words": account.get("stop_words", ""),
        "has_proxy": bool(account.get("proxy_url")),
        "last_synced_at": account.get("last_synced_at", ""),
        "next_scheduled_search_at": (next_search_time(scheduler) or account.get("next_scheduled_search_at", ""))
        if enabled
        else "",
        "resume_ready": bool(str(account.get("resume_text") or "").strip() and account.get("active_resume_hh_id")),
    }


def public_questionnaire(item: dict) -> dict:
    result = dict(item)
    for field, fallback in (("questions_json", []), ("ai_payload_json", {})):
        raw = result.pop(field, None)
        try:
            result[field.removesuffix("_json")] = json.loads(raw or "null") or fallback
        except (TypeError, json.JSONDecodeError):
            result[field.removesuffix("_json")] = fallback
    result.pop("resume_text", None)
    return result


__all__ = ["public_account", "public_questionnaire"]
