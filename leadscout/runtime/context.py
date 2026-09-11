"""Construct explicitly owned dependencies for a single LeadScout application."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

from leadscout.core import config
from leadscout.core.concurrency import Coordination
from leadscout.core.paths import WEB_DIST_DIR
from leadscout.integrations.ai import AIIntegration
from leadscout.integrations.application_forms import ApplicationClient
from leadscout.integrations.browser import HHBrowserEngine
from leadscout.integrations.browser_pool import SharedBrowserPool
from leadscout.integrations.login import HHLoginManager
from leadscout.integrations.resumes import HHResumeManager
from leadscout.integrations.vacancies import HHVacancyManager
from leadscout.services import Services, build_services
from leadscout.storage import Database
from leadscout.storage.facade import Storage
from utils.security import SessionSecurityManager

from .coordinator import TaskCoordinator
from .dependencies import RuntimeJobDependencies
from .operations import OperationManager


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    bot_token: str
    owner_telegram_id: int | None
    web_app_origins: tuple[str, ...]
    web_secure_cookies: bool
    web_session_ttl_sec: int
    pdf_max_bytes: int
    web_dist_dir: Path
    app_url: str = ""
    owner_telegram_ids: tuple[int, ...] = ()

    @property
    def allowed_owner_ids(self) -> tuple[int, ...]:
        return self.owner_telegram_ids or ((self.owner_telegram_id,) if self.owner_telegram_id else ())


@dataclass(slots=True)
class AppContext:
    db: Any
    coordinator: Any
    login_manager: Any
    resume_manager: Any
    ai: Any
    services: Services
    operations: OperationManager
    settings: RuntimeSettings
    locks: Coordination
    browser_pool: Any
    applications: Any
    security_factory: Any
    scheduler: Any = None
    bot: Any = None
    dispatcher: Any = None
    api_server: Any = None
    api_task: asyncio.Task | None = None
    serving_tasks: set[asyncio.Task] = field(default_factory=set)
    storage_ready: bool = False
    initialized: bool = False
    closing: bool = False
    shutdown_task: asyncio.Task | None = None
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def default_settings() -> RuntimeSettings:
    return RuntimeSettings(
        bot_token=config.BOT_TOKEN,
        owner_telegram_id=config.OWNER_TELEGRAM_ID,
        owner_telegram_ids=config.OWNER_TELEGRAM_IDS,
        web_app_origins=config.WEB_APP_ORIGINS,
        web_secure_cookies=config.WEB_SECURE_COOKIES,
        web_session_ttl_sec=config.WEB_SESSION_TTL_SEC,
        pdf_max_bytes=config.PDF_MAX_BYTES,
        web_dist_dir=WEB_DIST_DIR,
        app_url=config.APP_URL,
    )


def build_context(
    *,
    db=None,
    coordinator=None,
    login_manager=None,
    resume_manager=None,
    ai=None,
    settings=None,
    operations=None,
    locks=None,
    browser_pool=None,
    security_factory=None,
    applications=None,
    notifier=None,
) -> AppContext:
    if isinstance(coordinator, TaskCoordinator):
        supplied = dict(
            db=db, ai=ai, browser_pool=browser_pool, applications=applications, security_factory=security_factory
        )
        dependencies = coordinator._dependencies
        for name, value in supplied.items():
            expected = getattr(dependencies, name)
            if (
                value is not None
                and value is not expected
                and not (name == "db" and value is getattr(expected, "database", None))
            ):
                raise ValueError(f"Coordinator and AppContext must share {name}")
        if locks is not None and locks is not coordinator.locks:
            raise ValueError("Coordinator and AppContext must share locks")
        db, ai, browser_pool = dependencies.db, dependencies.ai, dependencies.browser_pool
        applications, security_factory = dependencies.applications, dependencies.security_factory
    if db is None:
        db = Database(config.DB_PATH)
    if isinstance(db, Database):
        db = Storage(db)
    settings = settings if settings is not None else default_settings()
    locks = locks if locks is not None else getattr(coordinator, "locks", None)
    locks = locks if locks is not None else Coordination(config.MAX_CONCURRENT_BROWSERS)
    engine_factory = partial(HHBrowserEngine, slots=locks.browser_slots)
    browser_pool = browser_pool if browser_pool is not None else SharedBrowserPool(engine_factory)
    security_factory = (
        security_factory
        if security_factory is not None
        else partial(SessionSecurityManager, config.SESSION_ENCRYPTION_KEY)
    )
    ai = ai if ai is not None else AIIntegration()
    applications = applications if applications is not None else ApplicationClient(ai)
    if coordinator is None:
        dependencies = RuntimeJobDependencies(db, ai, browser_pool, applications, security_factory, settings.app_url)
        coordinator = TaskCoordinator(dependencies=dependencies, locks=locks, notifier=notifier)
    elif notifier is not None:
        coordinator.configure_notifier(notifier)
    if login_manager is None:
        login_manager = HHLoginManager(
            db=db,
            engine_factory=engine_factory,
            security_factory=security_factory,
            locks=locks,
            stop_account=coordinator.stop_account,
        )
    if resume_manager is None:
        common = dict(db=db, browser_pool=browser_pool, security_factory=security_factory, locks=locks)
        vacancies = HHVacancyManager(**common)
        resume_manager = HHResumeManager(ai=ai, vacancies=vacancies, **common)
    services = build_services(
        db=db,
        coordinator=coordinator,
        login_manager=login_manager,
        resume_manager=resume_manager,
        ai=ai,
    )
    return AppContext(
        db=db,
        coordinator=coordinator,
        login_manager=login_manager,
        resume_manager=resume_manager,
        ai=ai,
        services=services,
        operations=operations if operations is not None else OperationManager(db),
        settings=settings,
        locks=locks,
        browser_pool=browser_pool,
        applications=applications,
        security_factory=security_factory,
    )


def build_default_context(*, settings=None) -> AppContext:
    """Create an independent graph; standard entrypoints use get_default_context."""
    return build_context(settings=settings)


_default_context: AppContext | None = None


def get_default_context() -> AppContext:
    global _default_context
    if _default_context is None:
        _default_context = build_context()
    return _default_context


__all__ = [
    "AppContext",
    "RuntimeSettings",
    "build_context",
    "build_default_context",
    "default_settings",
    "get_default_context",
]
