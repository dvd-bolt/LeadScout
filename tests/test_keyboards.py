from urllib.parse import parse_qs, urlsplit

from keyboards import get_entry_keyboard, get_questionnaire_confirmation_keyboard, get_stop_keyboard, mini_app_url


def test_entry_has_only_app_stop_and_help():
    rows = get_entry_keyboard(app_url="https://test.example/").keyboard
    assert rows[0][0].web_app.url.startswith("https://test.example/")
    assert [button.text for button in rows[1]] == ["⛔ Остановить", "❓ Помощь"]
    assert mini_app_url(app_url="http://insecure.example/") is None


def test_questionnaire_links_preserve_real_ids_and_query():
    markup = get_questionnaire_confirmation_keyboard(77, account_id=33, app_url="https://test.example/?ref=bot")
    button = markup.inline_keyboard[0][0]
    assert button.callback_data is None
    assert parse_qs(urlsplit(button.web_app.url).query) == {
        "ref": ["bot"],
        "target": ["questionnaire"],
        "apply_id": ["77"],
        "account_id": ["33"],
    }


def test_stop_buttons_use_real_ids():
    markup = get_stop_keyboard([{"id": 3}, {"id": 7}], app_url="https://test.example/")
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]
    assert callbacks == ["stop_account:3", "stop_account:7", "stop_all:confirm"]
