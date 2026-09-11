"""Offline smoke test for the built image: real runtime, HTTP, Chromium and SQLite.

Uses a temporary DB, a fake Telegram transport and fake hh.ru submission;
never logs in or sends applications/messages to external services.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from cryptography.fernet import Fernet

from leadscout.api.auth import SESSION_COOKIE, sign_session
from leadscout.runtime.context import build_context, default_settings
from leadscout.runtime.lifecycle import shutdown
from leadscout.runtime.runner import run_application
from leadscout.storage import Database
from utils.security import SessionSecurityManager


class OfflineDispatcher(dict):
    def __init__(self, *, storage):
        super().__init__()
        self.storage = storage

    def include_router(self, router):
        self.router = router

    async def start_polling(self, *args, **kwargs):
        await asyncio.Event().wait()


async def smoke(directory: Path) -> None:
    origin = "http://127.0.0.1:8000"
    settings = replace(
        default_settings(),
        bot_token="123456:offline-container-token",
        owner_telegram_id=42,
        owner_telegram_ids=(),
        web_app_origins=(origin,),
        web_secure_cookies=False,
    )
    key = Fernet.generate_key().decode()
    ai = SimpleNamespace(check_ai_capability=AsyncMock(return_value=True), close_ai_client=AsyncMock())
    context = build_context(
        db=Database(directory / "smoke.db"),
        settings=settings,
        ai=ai,
        security_factory=lambda: SessionSecurityManager(key),
    )
    bot = SimpleNamespace(session=SimpleNamespace(close=AsyncMock()), send_message=AsyncMock())
    runtime = asyncio.create_task(
        run_application(context, with_api=True, bot_factory=lambda **_: bot, dispatcher_factory=OfflineDispatcher)
    )
    entered = asyncio.Event()
    block_submission = False

    async def offline_submit(page, url, letter, answers, resume_id):
        assert resume_id == "smoke-resume"
        await page.set_content("<main>Offline Chromium fixture</main>")
        assert await page.locator("main").inner_text() == "Offline Chromium fixture"
        entered.set()
        if block_submission:
            await asyncio.Event().wait()
        return True, "Offline submission confirmed"

    context.applications.submit_approved_questionnaire = offline_submit
    try:
        async with httpx.AsyncClient(base_url=origin, timeout=10) as client:
            for _ in range(200):
                if runtime.done():
                    runtime.result()
                    raise RuntimeError("Runtime exited before readiness")
                try:
                    response = await client.get("/healthz")
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
            else:
                raise TimeoutError("HTTP readiness failed")
            assert response.json() == {"status": "ok"}
            document = await client.get("/")
            assert document.status_code == 200
            bundle = re.search(r'src="([^\"]+\.js)"', document.text)
            assert bundle and (await client.get(bundle[1])).status_code == 200
            account = await context.db.create_hh_account(42, "smoke@example.com")
            await context.db.update_account_session(
                42, account["id"], context.security_factory().encrypt_storage_state({}), "ACTIVE"
            )
            await context.db.update_account_settings_for_user(
                42,
                account["id"],
                active_resume_hh_id="smoke-resume",
                resume_text="Python and SQL",
                auto_apply_enabled=1,
            )
            client.cookies.set(
                SESSION_COOKIE,
                sign_session(
                    {"user_id": 42, "csrf": "smoke", "expires_at": int(time.time()) + 600}, settings.bot_token
                ),
            )
            client.headers.update({"Origin": origin, "X-CSRF-Token": "smoke"})
            for expected in ("SUBMITTED", "NEEDS_REVIEW"):
                entered.clear()
                block_submission = expected == "NEEDS_REVIEW"
                qid = await context.db.save_pending_questionnaire_account(
                    42,
                    account["id"],
                    f"https://hh.ru/vacancy/{1 if not block_submission else 2}",
                    "Offline role",
                    "Letter",
                    [],
                    {"answers": []},
                )
                saved = await client.patch(f"/api/v1/questionnaires/{qid}", json={"cover_letter": "Reviewed"})
                assert saved.status_code == 200 and saved.json()["revision"] == 1
                confirmed = await client.post(
                    f"/api/v1/questionnaires/{qid}/confirm", json={"expected_revision": saved.json()["revision"]}
                )
                assert confirmed.status_code == 202
                await asyncio.wait_for(entered.wait(), 30)
                if block_submission:
                    stopped = await client.post("/api/v1/automation/stop-all")
                    assert stopped.status_code == 200
                await asyncio.gather(*list(context.coordinator._questionnaire_tasks.values()))
                item = (await client.get(f"/api/v1/questionnaires/{qid}")).json()
                assert item["status"] == expected, item["status"]
                if not block_submission:
                    assert (await client.post(f"/api/v1/questionnaires/{qid}/confirm")).status_code == 409
            async with context.db.database.connection() as connection:
                assert (await (await connection.execute("PRAGMA integrity_check")).fetchone())[0] == "ok"
    finally:
        await shutdown(context)
        await asyncio.gather(runtime, return_exceptions=True)
    bot.session.close.assert_awaited_once()
    ai.close_ai_client.assert_awaited_once()
    assert not context.serving_tasks
    print(
        json.dumps(
            {
                "offline_smoke": "passed",
                "sqlite_schema": 6,
                "checks": [
                    "shared runtime",
                    "HTTP health",
                    "built frontend",
                    "Chromium",
                    "questionnaire submission",
                    "duplicate rejection",
                    "stop",
                    "cleanup",
                ],
            }
        )
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="leadscout-container-smoke-") as directory:
        asyncio.run(smoke(Path(directory)))


if __name__ == "__main__":
    main()
