"""Cooperative access checks at resource boundaries without global context lookup."""

from contextvars import ContextVar

check_access = ContextVar("leadscout_task_access", default=None)


async def checkpoint():
    callback = check_access.get()
    if callback:
        await callback()


track_resource = ContextVar("leadscout_track_resource", default=None)


def register_resource(resource):
    callback = track_resource.get()
    if callback:
        callback(resource)
