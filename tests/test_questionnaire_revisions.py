from __future__ import annotations

import json

import database
import web_api


async def make_questionnaire():
    account = await database.create_hh_account(42, "revision@example.com")
    return await database.save_pending_questionnaire_account(
        42,
        account["id"],
        "https://hh.ru/vacancy/123",
        "Role",
        "Letter",
        [{"field_id": "q0", "label": "Experience", "answer_type": "text", "required": True}],
        {"answers": [{"field_id": "q0", "answer_type": "text", "value": "Python"}]},
    )


async def test_draft_clear_omit_and_confirmation_validation(audit_client):
    qid = await make_questionnaire()
    url = f"/api/v1/questionnaires/{qid}"
    first = (await audit_client.get(url)).json()
    cleared = await audit_client.patch(url, json={"answers": [], "cover_letter": "Draft"})
    assert cleared.status_code == 200
    assert cleared.json()["revision"] == first["revision"] + 1
    changed = await audit_client.patch(url, json={"cover_letter": "Later"})
    assert changed.json()["ai_payload"]["answers"] == []
    assert changed.json()["revision"] == cleared.json()["revision"] + 1
    assert (await audit_client.post(url + "/confirm")).status_code == 422
    assert (await database.get_pending_questionnaire_for_user(42, qid))["status"] == "PENDING"


async def test_stale_confirmation_does_not_claim(audit_client):
    qid = await make_questionnaire()
    url = f"/api/v1/questionnaires/{qid}"
    old = (await audit_client.get(url)).json()["revision"]
    await audit_client.patch(url, json={"cover_letter": "Changed by another request"})
    response = await audit_client.post(url + "/confirm", json={"expected_revision": old})
    assert response.status_code == 409
    assert (await database.get_pending_questionnaire_for_user(42, qid))["status"] == "PENDING"


async def test_edit_between_validation_and_claim_cannot_submit(audit_client, monkeypatch):
    qid = await make_questionnaire()
    original = web_api.task_coordinator.start_questionnaire

    async def concurrent_edit(user_id, apply_id, *, expected_revision=None):
        await database.edit_pending_questionnaire(user_id, apply_id, "Unfinished", [])
        return await original(user_id, apply_id, expected_revision=expected_revision)

    monkeypatch.setattr(web_api.task_coordinator, "start_questionnaire", concurrent_edit)
    response = await audit_client.post(f"/api/v1/questionnaires/{qid}/confirm")
    assert response.status_code == 409
    item = await database.get_pending_questionnaire_for_user(42, qid)
    assert item["status"] == "PENDING"
    assert json.loads(item["ai_payload_json"])["answers"] == []
    assert not web_api.task_coordinator.is_questionnaire_running(42, qid)
