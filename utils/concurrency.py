"""Compatibility imports for context-owned synchronization."""

from leadscout.core.concurrency import AccountLock, serialize_account, serialize_login
from leadscout.runtime.context import get_default_context

account_locks: dict
login_locks: dict


def __getattr__(name):
    if name in {"account_locks", "login_locks"}:
        return getattr(get_default_context().locks, name)
    raise AttributeError(name)


__all__ = ["AccountLock", "serialize_account", "serialize_login", "account_locks", "login_locks"]
