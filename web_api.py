"""Legacy ASGI entrypoint and helpers using the default application context."""

from fastapi import FastAPI

from leadscout.api import create_app as create_package_app
from leadscout.api.auth import SESSION_COOKIE, decode_session, sign_session, validate_telegram_init_data
from leadscout.api.presenters import public_account
from leadscout.api.routes.audits import temporary_pdf as _temporary_pdf
from leadscout.compat import ContextProxy
from leadscout.core.paths import WEB_DIST_DIR
from leadscout.runtime.context import get_default_context

task_coordinator = ContextProxy("coordinator")
HHLoginManager = ContextProxy("login_manager")
HHResumeManager = ContextProxy("resume_manager")


def create_context():
    return get_default_context()


def create_app(context=None):
    return create_package_app(context if context is not None else get_default_context())


def _sign_session(payload):
    return sign_session(payload, get_default_context().settings.bot_token)


def _decode_session(token):
    return decode_session(token, get_default_context().settings.bot_token)


def _validate_telegram_init_data(init_data):
    settings = get_default_context().settings
    return validate_telegram_init_data(
        init_data, bot_token=settings.bot_token, owner_telegram_ids=settings.allowed_owner_ids
    )


def _public_account(account):
    context = get_default_context()
    return public_account(account, context.coordinator, context.scheduler)


async def _run_operation(operation_id, user_id, job):
    await get_default_context().operations._run(operation_id, user_id, job)


async def schedule_operation(user_id, kind, job, resource=None):
    return await get_default_context().operations.schedule(user_id, kind, job, resource)


async def shutdown_operations():
    await get_default_context().operations.shutdown()


app: FastAPI


def __getattr__(name):
    if name == "app":
        application = create_app()
        return application
    if name in {"_operation_tasks", "_operation_resources", "_operation_lock"}:
        attribute = {"_operation_tasks": "tasks", "_operation_resources": "resources", "_operation_lock": "lock"}[name]
        return getattr(get_default_context().operations, attribute)
    raise AttributeError(name)


__all__ = [
    "app",
    "create_app",
    "create_context",
    "SESSION_COOKIE",
    "WEB_DIST_DIR",
    "HHLoginManager",
    "HHResumeManager",
    "task_coordinator",
    "_temporary_pdf",
]
