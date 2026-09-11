"""Administrative routes expose only explicitly selected technical metadata."""

from typing import Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from leadscout.core.access import AccessError, telegram_id
from leadscout.services.admin import public_member, public_record

from ..dependencies import current_user, get_context, require_csrf

router = APIRouter(prefix="/admin", tags=["admin"])


class Revision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1, strict=True)


class AddMember(BaseModel):
    model_config = ConfigDict(extra="forbid")
    telegram_id: str = Field(min_length=1, max_length=19, pattern=r"^[0-9]+$")
    role: Literal["ADMIN", "USER"] = "USER"
    display_label: str = Field(default="", max_length=100)


class PatchMember(Revision):
    role: Literal["ADMIN", "USER"] | None = None
    display_label: str | None = Field(default=None, max_length=100)


async def admin(session=Depends(current_user), context=Depends(get_context)):
    return await context.access.require(int(session["user_id"]), roles=("ROOT", "ADMIN"))


async def root(session=Depends(current_user), context=Depends(get_context)):
    return await context.access.require(int(session["user_id"]), roles=("ROOT",))


async def key(idempotency_key: str = Header(min_length=8, max_length=128)):
    return idempotency_key


@router.get("/overview")
async def overview(_=Depends(admin), context=Depends(get_context)):
    return await context.admin.overview()


@router.get("/members")
async def members(
    search: str = Query(default="", max_length=100),
    role: Literal["ROOT", "ADMIN", "USER"] | None = None,
    access_status: Literal["ACTIVE", "BLOCKED"] | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    _=Depends(root),
    context=Depends(get_context),
):
    rows = await context.admin_store.list_members(
        search=search, role=role, access_status=access_status, offset=offset, limit=limit + 1
    )
    pending = await context.admin_store.rows(
        "SELECT id,target_id,status FROM admin_actions WHERE kind='BLOCK' AND status!='SUCCEEDED'"
    )
    for row in rows:
        action = next((a for a in pending if a["target_id"] == row["telegram_id"]), None)
        row["cleanup_action_id"] = action["id"] if action else None
    return {
        "items": [public_member(row) for row in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@router.get("/members/{member_id}")
async def member(member_id: str, _=Depends(root), context=Depends(get_context)):
    target = telegram_id(member_id)
    rows = await context.admin_store.rows(
        "SELECT m.*, (SELECT COUNT(*) FROM hh_accounts a WHERE a.user_id=m.telegram_id) AS account_count, (SELECT COUNT(*) FROM admin_tasks t WHERE t.user_id=m.telegram_id AND t.status IN ('QUEUED','RUNNING','WAITING_INPUT','STOPPING')) AS task_count FROM access_members m WHERE m.telegram_id=?",
        (target,),
    )
    row = rows[0] if rows else None
    if not row:
        raise AccessError("NOT_FOUND", "Человек не найден.", 404)
    return public_member(row)


@router.post("/members", status_code=201)
async def add_member(
    payload: AddMember,
    session=Depends(require_csrf),
    _=Depends(root),
    request_key=Depends(key),
    context=Depends(get_context),
):
    return await context.admin.add_member(int(session["user_id"]), payload.model_dump(), request_key)


@router.patch("/members/{member_id}")
async def patch_member(
    member_id: str,
    payload: PatchMember,
    session=Depends(require_csrf),
    _=Depends(root),
    request_key=Depends(key),
    context=Depends(get_context),
):
    return public_record(
        await context.admin.change_member(
            int(session["user_id"]), member_id, payload.model_dump(exclude_none=True), "PATCH_MEMBER", request_key
        )
    )


@router.post("/members/{member_id}/block", status_code=202)
async def block(
    member_id: str,
    payload: Revision,
    session=Depends(require_csrf),
    _=Depends(root),
    request_key=Depends(key),
    context=Depends(get_context),
):
    action = await context.admin.change_member(
        int(session["user_id"]), member_id, payload.model_dump(), "BLOCK", request_key
    )
    return {"action_id": action["id"], **public_record(action)}


@router.post("/members/{member_id}/restore")
async def restore(
    member_id: str,
    payload: Revision,
    session=Depends(require_csrf),
    _=Depends(root),
    request_key=Depends(key),
    context=Depends(get_context),
):
    return public_record(
        await context.admin.change_member(
            int(session["user_id"]), member_id, payload.model_dump(), "RESTORE", request_key
        )
    )


@router.get("/tasks")
async def tasks(
    status: Literal["QUEUED", "RUNNING", "WAITING_INPUT", "STOPPING", "SUCCEEDED", "FAILED", "CANCELLED", "INTERRUPTED"]
    | None = None,
    user_id: str | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    actor=Depends(admin),
    context=Depends(get_context),
):
    where, args = ["1=1"], []
    if status:
        where.append("status=?")
        args.append(status)
    if user_id:
        where.append("user_id=?")
        args.append(telegram_id(user_id))
    rows = await context.admin_store.rows(
        f"SELECT * FROM admin_tasks WHERE {' AND '.join(where)} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
        (*args, limit + 1, offset),
    )
    root_id = (await context.admin_store.rows("SELECT telegram_id FROM access_members WHERE role='ROOT'"))[0][
        "telegram_id"
    ]
    items = []
    for row in rows[:limit]:
        item = public_record(row)
        item["can_stop"] = row["status"] in ("QUEUED", "RUNNING", "WAITING_INPUT", "STOPPING") and (
            actor["role"] == "ROOT" or row["user_id"] != root_id
        )
        items.append(item)
    predicate = "1=1" if actor["role"] == "ROOT" else "user_id!=?"
    parameters = () if actor["role"] == "ROOT" else (root_id,)
    counts = await context.admin_store.rows(
        f"SELECT COUNT(*) AS tasks,COUNT(DISTINCT user_id) AS users FROM admin_tasks WHERE status IN ('QUEUED','RUNNING','WAITING_INPUT','STOPPING') AND {predicate}",
        parameters,
    )
    affected_users = await context.admin_store.rows(
        f"SELECT COUNT(*) AS users FROM (SELECT user_id FROM admin_tasks WHERE status IN ('QUEUED','RUNNING','WAITING_INPUT','STOPPING') AND {predicate} UNION SELECT user_id FROM hh_accounts WHERE auto_apply_enabled=1 AND {predicate})",
        (*parameters, *parameters),
    )
    counts[0]["users"] = affected_users[0]["users"]
    automated = await context.admin_store.rows(
        f"SELECT COUNT(*) AS accounts FROM hh_accounts WHERE auto_apply_enabled=1 AND {predicate}", parameters
    )
    return {
        "items": items,
        "next_offset": offset + limit if len(rows) > limit else None,
        "stop_scope": {**counts[0], **automated[0]},
    }


@router.post("/tasks/stop-all", status_code=202)
async def stop_all(
    session=Depends(require_csrf), _=Depends(admin), request_key=Depends(key), context=Depends(get_context)
):
    action = await context.admin.stop(int(session["user_id"]), request_key)
    return {"action_id": action["id"], **public_record(action)}


@router.post("/tasks/{task_id}/stop", status_code=202)
async def stop_task(
    task_id: str,
    session=Depends(require_csrf),
    _=Depends(admin),
    request_key=Depends(key),
    context=Depends(get_context),
):
    action = await context.admin.stop(int(session["user_id"]), request_key, task_id)
    return {"action_id": action["id"], **public_record(action)}


@router.get("/actions")
async def actions(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    _=Depends(admin),
    context=Depends(get_context),
):
    rows = await context.admin_store.rows(
        "SELECT * FROM admin_actions ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (limit + 1, offset)
    )
    return {
        "items": [public_record(context.admin_store.decode_action(r)) for r in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@router.get("/actions/{action_id}")
async def action(action_id: str, _=Depends(admin), context=Depends(get_context)):
    row = await context.admin_store.action(action_id)
    if not row:
        raise AccessError("NOT_FOUND", "Действие не найдено.", 404)
    return public_record(row)


@router.post("/actions/{action_id}/retry-cleanup", status_code=202)
async def retry(
    action_id: str,
    session=Depends(require_csrf),
    _=Depends(admin),
    request_key=Depends(key),
    context=Depends(get_context),
):
    return public_record(await context.admin.retry(int(session["user_id"]), action_id, request_key))


@router.get("/errors")
async def errors(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    _=Depends(admin),
    context=Depends(get_context),
):
    rows = await context.admin_store.rows(
        "SELECT * FROM admin_errors ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?", (limit + 1, offset)
    )
    return {
        "items": [public_record(r) for r in rows[:limit]],
        "next_offset": offset + limit if len(rows) > limit else None,
    }


@router.get("/invitation")
async def invitation(_=Depends(root), context=Depends(get_context)):
    if context.bot is None:
        return {"bot_url": ""}
    identity = await context.bot.me()
    return {"bot_url": f"https://t.me/{identity.username}" if identity.username else ""}
