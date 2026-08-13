from __future__ import annotations

from types import SimpleNamespace

import pytest

import handlers


class FakeState:
    def __init__(self, data=None):
        self.data = dict(data or {})

    async def update_data(self, **values):
        self.data.update(values)

    async def get_data(self):
        return dict(self.data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "expected_text"),
    [
        (handlers.cmd_accounts_and_resume_hub, "Не авторизован"),
        (handlers.cmd_settings_and_analytics_hub, "Должность: Не авторизован"),
    ],
)
async def test_hubs_render_without_an_active_account(monkeypatch, handler, expected_text):
    sent: list[str] = []

    async def no_active_account(user_id):
        return None

    async def capture_banner(target, banner_path, text, **kwargs):
        sent.append(text)

    monkeypatch.setattr(handlers, "get_active_account", no_active_account)
    monkeypatch.setattr(handlers, "send_banner_message", capture_banner)

    message = SimpleNamespace(from_user=SimpleNamespace(id=42))
    await handler(message, FakeState())

    assert expected_text in sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("nav_hub", ["accounts", "settings"])
async def test_back_navigation_renders_without_an_active_account(monkeypatch, nav_hub):
    sent: list[str] = []

    async def no_active_account(user_id):
        return None

    async def capture_banner(target, banner_path, text, **kwargs):
        sent.append(text)

    async def answer(*args, **kwargs):
        return None

    monkeypatch.setattr(handlers, "get_active_account", no_active_account)
    monkeypatch.setattr(handlers, "send_banner_message", capture_banner)

    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        answer=answer,
    )
    await handlers.cb_nav_back(callback, FakeState({"nav_hub": nav_hub}))

    assert "Не авторизован" in sent[0]
