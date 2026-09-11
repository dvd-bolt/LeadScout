"""Legacy login entrypoints using explicitly owned session dependencies."""

from leadscout.compat import ContextProxy
from leadscout.integrations.login import HHLoginSession as LoginSession
from leadscout.runtime.context import get_default_context

HHLoginManager = ContextProxy("login_manager")


class HHLoginSession(LoginSession):
    def __init__(self, user_id, phone_or_email, account_id=None, **kwargs):
        context = get_default_context()
        kwargs.setdefault("db", context.db)
        kwargs.setdefault("engine_factory", context.login_manager.engine_factory)
        kwargs.setdefault("security_factory", context.security_factory)
        super().__init__(user_id, phone_or_email, account_id, **kwargs)


__all__ = ["HHLoginManager", "HHLoginSession"]
