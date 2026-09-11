from urllib.parse import parse_qs, urlsplit

from leadscout.bot.keyboards import get_questionnaire_confirmation_keyboard, mini_app_url


def test_deep_links_keep_query_before_hash_and_address_objects():
    url = mini_app_url(
        "resume",
        app_url="https://mini.example/base#/existing",
        account_id=7,
        snapshot_id=55,
    )
    split = urlsplit(url)
    assert split.fragment == "/existing"
    assert parse_qs(split.query) == {
        "target": ["resume"],
        "account_id": ["7"],
        "snapshot_id": ["55"],
    }


def test_questionnaire_keyboard_only_opens_review_screen():
    markup = get_questionnaire_confirmation_keyboard(12, app_url="https://mini.example", account_id=3)
    buttons = [button for row in markup.inline_keyboard for button in row]
    assert len(buttons) == 1
    assert buttons[0].callback_data is None
    query = parse_qs(urlsplit(buttons[0].web_app.url).query)
    assert query == {
        "target": ["questionnaire"],
        "account_id": ["3"],
        "apply_id": ["12"],
    }
