"""Legacy browser constructors sharing the default context's capacity."""

from leadscout.compat import ContextProxy
from leadscout.integrations.browser import HHBrowserEngine as BrowserEngine
from leadscout.integrations.browser import _proxy_config, intercept_network_traffic
from leadscout.runtime.context import get_default_context

SharedBrowserPool = ContextProxy("browser_pool")


class HHBrowserEngine(BrowserEngine):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("slots", get_default_context().locks.browser_slots)
        super().__init__(*args, **kwargs)


def __getattr__(name):
    if name == "_context_slots":
        return get_default_context().locks.browser_slots
    raise AttributeError(name)


__all__ = ["HHBrowserEngine", "SharedBrowserPool", "_proxy_config", "intercept_network_traffic"]
