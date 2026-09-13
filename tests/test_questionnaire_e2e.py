"""Built React → ASGI API → services → real jobs/coordinator → SQLite."""

import asyncio

import pytest
from patchright.async_api import expect

from leadscout.diagnostics import ApplicationAttemptTracer
from leadscout.runtime import build_context
from leadscout.runtime.lifecycle import initialize, shutdown


async def ready_questionnaire(app):
    context = app.runtime
    account_id = app.account["id"]
    await context.db.update_account_session(
        42, account_id, context.security_factory().encrypt_storage_state({}), "ACTIVE"
    )
    return await context.db.save_pending_questionnaire_account(
        42, account_id, "https://hh.ru/vacancy/891", "Full pipeline", "Original letter", [], {"answers": []}
    )


@pytest.mark.parametrize("mini_app", ["real_coordinator"], indirect=True)
@pytest.mark.parametrize("outcome", ["success", "failure", "stop", "lost_response"])
async def test_direct_link_long_running_submission(mini_app, external_submission, outcome):
    app, external = mini_app, external_submission
    qid = await ready_questionnaire(app)
    page, context = app.page, app.runtime
    external.success = outcome != "failure"
    reads = []
    page.on("request", lambda request: reads.append(request.url) if request.method == "GET" else None)
    if outcome == "lost_response":

        async def lose_response(route):
            request = route.request
            accepted = await app.client.post(request.url, headers=request.headers, content=request.post_data_buffer)
            assert accepted.status_code == 202
            await route.abort("failed")

        await page.route(f"**/api/v1/questionnaires/{qid}/confirm", lose_response)
    await page.goto(f"http://leadscout.test/?target=questionnaire&apply_id={qid}#/")
    await page.get_by_label("Сопроводительное письмо", exact=False).fill("Reviewed letter")
    await page.get_by_role("button", name="Подтвердить отправку", exact=True).click()
    await asyncio.wait_for(external.entered.wait(), 5)
    await expect(page.get_by_text("Отправляется", exact=True)).to_be_visible()
    await expect(page.get_by_role("button", name="Подтвердить отправку", exact=True)).to_be_disabled()
    await expect(page.get_by_label("Сопроводительное письмо", exact=False)).to_be_disabled()
    item = await context.db.get_pending_questionnaire_for_user(42, qid)
    assert item["revision"] == 1 and item["cover_letter"] == "Reviewed letter"
    repeat = await app.client.post(f"/api/v1/questionnaires/{qid}/confirm", json={"expected_revision": 1})
    assert repeat.status_code == 202 and repeat.json()["status"] == "ALREADY_RUNNING"
    before = len([url for url in reads if url.endswith(f"/questionnaires/{qid}")])
    await page.wait_for_timeout(3300)
    assert len([url for url in reads if url.endswith(f"/questionnaires/{qid}")]) > before
    assert len(external.calls) == 1
    if outcome == "stop":
        response = await app.client.post("/api/v1/automation/stop-all")
        assert response.status_code == 200
    else:
        external.release.set()
    expected = {"failure": "Ошибка отправки", "stop": "Ошибка отправки"}.get(outcome, "Отправлена")
    await expect(page.get_by_text(expected, exact=True)).to_be_visible(timeout=7000)
    assert len(external.calls) == 1
    if outcome in {"success", "lost_response"}:
        await expect(page.get_by_role("link", name="Отклики", exact=True)).to_be_visible(timeout=5000)
        assert (await app.client.post(f"/api/v1/questionnaires/{qid}/confirm")).status_code == 409
        assert (await context.db.get_account_for_user(42, app.account["id"]))["applied_today"] == 1
    before = len([url for url in reads if url.endswith(f"/questionnaires/{qid}")])
    await page.wait_for_timeout(3500)
    assert len([url for url in reads if url.endswith(f"/questionnaires/{qid}")]) == before
    assert len(external.calls) == 1
    external.browser_context.close.assert_awaited_once()


@pytest.mark.parametrize("mini_app", ["real_coordinator"], indirect=True)
async def test_conflict_refreshes_draft_without_automatic_resubmission(mini_app, external_submission, monkeypatch):
    qid = await ready_questionnaire(mini_app)
    service = mini_app.runtime.services.questionnaires
    original = service.confirm

    async def edit_then_confirm(user_id, apply_id, expected_revision=None):
        await mini_app.runtime.db.edit_pending_questionnaire(user_id, apply_id, "Concurrent draft", [])
        return await original(user_id, apply_id, expected_revision)

    monkeypatch.setattr(service, "confirm", edit_then_confirm)
    page = mini_app.page
    await page.goto(f"http://leadscout.test/?target=questionnaire&apply_id={qid}#/")
    await page.get_by_role("button", name="Подтвердить отправку", exact=True).click()
    await expect(page.get_by_role("status")).to_contain_text("Анкета уже изменилась")
    await expect(page.get_by_label("Сопроводительное письмо", exact=False)).to_have_value("Concurrent draft")
    assert not external_submission.calls
    assert (await mini_app.runtime.db.get_pending_questionnaire_for_user(42, qid))["status"] == "PENDING"


@pytest.mark.parametrize("mini_app", ["real_coordinator"], indirect=True)
async def test_reopen_incomplete_draft_and_recover_interruption(mini_app, external_submission):
    qid = await ready_questionnaire(mini_app)
    context, page = mini_app.runtime, mini_app.page
    await page.goto(f"http://leadscout.test/?target=questionnaire&apply_id={qid}#/")
    await page.get_by_label("Сопроводительное письмо", exact=False).fill("Saved before restart")
    await page.get_by_role("button", name="Сохранить", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Анкета сохранена")
    await page.reload()
    await expect(page.get_by_label("Сопроводительное письмо", exact=False)).to_have_value("Saved before restart")
    # Durable state left by an interrupted process before its job ran.
    item = await context.db.get_pending_questionnaire_for_user(42, qid)
    assert await context.db.claim_pending_questionnaire(42, qid, expected_revision=item["revision"])
    restarted = build_context(db=context.db.database, settings=context.settings)
    try:
        assert (await initialize(restarted))[1] == 1
        await page.reload()
        await expect(page.get_by_text("Нужна проверка", exact=True)).to_be_visible()
        await expect(page.get_by_label("Сопроводительное письмо", exact=False)).to_have_value("Saved before restart")
        assert not external_submission.calls
    finally:
        await shutdown(restarted)


@pytest.mark.parametrize("mini_app", ["real_coordinator"], indirect=True)
async def test_history_resolves_uncertain_attempt_and_unblocks_retry(mini_app):
    account = mini_app.account
    tracer = await ApplicationAttemptTracer.start(
        mini_app.runtime.db,
        42,
        account["id"],
        "https://hh.ru/vacancy/998",
        "Uncertain role",
    )
    assert tracer is not None
    await tracer.stage("SUBMITTING")
    reason = await tracer.finish("ERROR_SUBMIT_UNCONFIRMED")
    await mini_app.runtime.db.record_application_event(
        42,
        account["id"],
        "998",
        "ERROR_SUBMIT_UNCONFIRMED",
        "Uncertain role",
        details=reason,
        attempt_id=tracer.attempt_id,
        stage="SUBMITTING",
    )

    page = mini_app.page
    await page.goto("http://leadscout.test/#/applications?tab=history")
    await page.get_by_role("button", name="Отклика нет", exact=True).click()

    await expect(page.get_by_text("Проверено: отклик не отправлен", exact=True)).to_be_visible()
    assert not await mini_app.runtime.db.has_unresolved_application_attempt(42, account["id"], "998")
