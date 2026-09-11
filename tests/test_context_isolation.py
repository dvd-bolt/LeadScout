import ast
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from leadscout.api import create_app
from leadscout.models.resumes import SearchKeywordsPayload
from leadscout.runtime.context import build_context, default_settings
from leadscout.runtime.lifecycle import initialize, shutdown
from leadscout.storage import Database


async def test_two_contexts_isolate_storage_services_integrations_and_resources(tmp_path, runtime_context, monkeypatch):
    contexts = [
        build_context(
            db=Database(tmp_path / f"isolated-{i}.db"),
            security_factory=runtime_context.security_factory,
            settings=replace(default_settings(), app_url=f"https://app-{i}.example/"),
        )
        for i in range(2)
    ]
    for context in contexts:
        await initialize(context)
    try:
        first, second = contexts
        a = await first.db.create_hh_account(42, "first@example.com")
        b = await second.db.create_hh_account(42, "second@example.com")
        assert a["id"] == b["id"]
        await first.services.accounts.update_settings(42, a["id"], {"daily_limit": 12})
        assert (await second.db.get_account_for_user(42, b["id"]))["daily_limit"] == 50
        assert first.locks is not second.locks
        assert first.locks.account_locks[a["id"]] is not second.locks.account_locks[b["id"]]
        assert first.browser_pool is not second.browser_pool
        assert first.ai.client is not second.ai.client
        first.ai.cache.put("key", SearchKeywordsPayload(keywords=["Python"]))
        assert second.ai.cache.get("key", SearchKeywordsPayload) is None
        for context, account in zip(contexts, [a, b], strict=True):
            dependencies = context.coordinator._dependencies
            assert dependencies.db is context.db
            assert dependencies.ai is context.ai
            assert dependencies.browser_pool is context.browser_pool
            assert dependencies.applications is context.applications
            assert context.operations.db is context.db
            assert context.login_manager.db is context.db
            assert context.resume_manager.db is context.db
            assert context.resume_manager.browser_pool is context.browser_pool
            assert context.login_manager.locks is context.locks
            assert context.resume_manager.locks is context.locks
            assert create_app(context).state.context is context
            encrypted = context.security_factory().encrypt_storage_state({"test": account["phone_or_email"]})
            await context.db.update_account_session(42, account["id"], encrypted, "ACTIVE")
            actual, state = await context.resume_manager._account_and_state(42, account["id"])
            assert actual["phone_or_email"] == account["phone_or_email"] == state["test"]
        browser = SimpleNamespace(storage_state=AsyncMock(return_value={"test": "new first state"}))
        await first.resume_manager._persist_context(42, a["id"], browser)
        _, unchanged = await second.resume_manager._account_and_state(42, b["id"])
        assert unchanged == {"test": "second@example.com"}
        assert await runtime_context.db.get_user_accounts(42) == []
        await first.coordinator.stop_account(42, a["id"])
        assert not second.coordinator._shutting_down
        await shutdown(first)
        assert not second.browser_pool._closed
        assert second.operations.accepting
    finally:
        for context in contexts:
            await shutdown(context)


def test_package_has_no_legacy_imports_or_module_registry_lookup():
    root = Path(__file__).resolve().parents[1] / "leadscout"
    forbidden = {
        "database",
        "worker",
        "handlers",
        "keyboards",
        "ai_handler",
        "parsers",
        "config",
        "web_api",
        "scheduler_app",
    }
    violations = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imports = [node.module or ""]
            else:
                imports = []
            if any(name.split(".")[0] in forbidden for name in imports):
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "sys"
                and node.attr == "modules"
            ):
                violations.append(f"{path.relative_to(root)}:{node.lineno}")
    assert violations == []


async def test_notification_links_use_injected_context_settings(tmp_path, runtime_context):
    context = build_context(
        db=Database(tmp_path / "notify.db"), settings=replace(default_settings(), app_url="https://isolated.example/")
    )
    sent = []

    class Notifier:
        async def send(self, user_id, notification):
            sent.append(notification)

    context.coordinator.configure_notifier(Notifier())
    try:
        await context.coordinator._notify_questionnaire(42, 7, "Account", "https://hh.ru/vacancy/1", "Role", {})
        markup = sent[0].reply_markup
        assert markup and markup.inline_keyboard[0][0].web_app.url.startswith("https://isolated.example/")
    finally:
        await shutdown(context)


def test_context_rejects_coordinator_from_a_different_database(tmp_path, runtime_context):
    import pytest

    with pytest.raises(ValueError, match="share db"):
        build_context(db=Database(tmp_path / "wrong.db"), coordinator=runtime_context.coordinator)
