"""Lazy forwarding used only by legacy entrypoint adapters."""

from leadscout.runtime.context import get_default_context


class ContextProxy:
    def __init__(self, attribute):
        object.__setattr__(self, "_attribute", attribute)

    def __getattr__(self, name):
        return getattr(getattr(get_default_context(), self._attribute), name)

    def __setattr__(self, name, value):
        setattr(getattr(get_default_context(), self._attribute), name, value)
