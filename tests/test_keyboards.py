from __future__ import annotations

from keyboards import (
    AUTOAPPLY_OFF_TEXT,
    AUTOAPPLY_ON_TEXT,
    get_main_keyboard,
    get_resume_audit_result_keyboard,
    get_resume_inline_keyboard,
    get_settings_analytics_hub_keyboard,
)


def _callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def test_main_button_texts_are_stable():
    assert get_main_keyboard(False).keyboard[0][0].text == AUTOAPPLY_OFF_TEXT
    assert get_main_keyboard(True).keyboard[0][0].text == AUTOAPPLY_ON_TEXT


def test_audit_and_resume_callbacks_use_real_ids():
    callbacks = _callbacks(get_resume_audit_result_keyboard(77))
    assert "show_audit_insights_77" in callbacks
    assert "match_with_vacancy_77" in callbacks

    resume_markup = get_resume_inline_keyboard(
        [{"snapshot_id": 33, "title": "Backend", "href": "https://hh.ru/resume/x"}]
    )
    assert "manage_res_33" in _callbacks(resume_markup)


def test_settings_hub_does_not_toggle_autoapply_as_a_threads_control():
    callbacks = _callbacks(get_settings_analytics_hub_keyboard({"proxy_url": ""}))
    assert "open_settings_menu_hub" in callbacks
    assert "toggle_account_auto_apply" not in callbacks
