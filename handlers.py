"""Compatibility entrypoints for the compact bot; business flows live in Mini App."""

from leadscout.bot.handlers import create_handlers_router, owner_only_factory
from leadscout.bot.router import create_router, router

__all__ = ["create_handlers_router", "create_router", "owner_only_factory", "router"]
