from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio
from patchright.async_api import async_playwright

import utils.humanization as humanization
from utils.humanization import HumanizationError, human_click, human_scroll, human_type, human_type_digits


@pytest_asyncio.fixture
async def chromium_page():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page(viewport={"width": 900, "height": 700})
    try:
        yield page
    finally:
        await browser.close()
        await playwright.stop()


@pytest.mark.asyncio
async def test_click_waits_for_a_moving_target_without_clicking_the_decoy(chromium_page):
    await chromium_page.set_content(
        """
        <button id="decoy" onclick="window.decoyClicks=(window.decoyClicks||0)+1">decoy</button>
        <div id="stage"></div>
        <script>
          setTimeout(() => {
            stage.innerHTML = '<button id="target" style="position:absolute;left:100px" onclick="window.targetClicks=(window.targetClicks||0)+1">target</button>';
            const target = document.querySelector('#target');
            target.onpointermove = () => {
              target.onpointermove = null;
              target.style.left = '520px';
            };
          }, 150);
        </script>
        """
    )

    await human_click(chromium_page, "#target", timeout_ms=2_000)

    assert await chromium_page.evaluate("window.targetClicks || 0", isolated_context=False) == 1
    assert await chromium_page.evaluate("window.decoyClicks || 0", isolated_context=False) == 0


@pytest.mark.asyncio
async def test_click_reports_an_overlay_instead_of_clicking_through_it(chromium_page):
    await chromium_page.set_content(
        """
        <button id="target" onclick="window.targetClicks=(window.targetClicks||0)+1">target</button>
        <div style="position:fixed;inset:0;z-index:2"></div>
        """
    )

    with pytest.raises(HumanizationError, match="click:action_not_confirmed"):
        await human_click(chromium_page, "#target", timeout_ms=300)
    assert await chromium_page.evaluate("window.targetClicks || 0", isolated_context=False) == 0


@pytest.mark.asyncio
async def test_click_does_not_wait_for_coordinates_after_target_disappears(chromium_page):
    await chromium_page.set_content(
        '<button id="target" onclick="this.hidden=true;window.clicked=true">target</button>'
    )

    started = time.monotonic()
    await human_click(chromium_page, "#target", timeout_ms=2_000)

    assert time.monotonic() - started < 1
    assert await chromium_page.evaluate("window.clicked", isolated_context=False)


@pytest.mark.asyncio
async def test_text_is_typed_sequentially_and_preserves_unicode(chromium_page, monkeypatch):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    text = "Привет 👋\r\ncafe\u0301"
    await chromium_page.set_content(
        """
        <textarea id="value"></textarea>
        <script>
          window.inputEvents = 0;
          document.querySelector('#value').addEventListener('input', () => window.inputEvents += 1);
        </script>
        """
    )

    await human_type(chromium_page, "#value", text)

    assert await chromium_page.locator("#value").input_value() == text.replace("\r\n", "\n")
    assert await chromium_page.evaluate("window.inputEvents", isolated_context=False) >= len(text) - 1


@pytest.mark.asyncio
async def test_long_text_mask_and_focus_loss_are_verified(chromium_page, monkeypatch):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    long_text = "абв" * 1_000
    await chromium_page.set_content('<textarea id="long"></textarea>')
    await human_type(chromium_page, "#long", long_text)
    assert await chromium_page.locator("#long").input_value() == long_text

    await chromium_page.set_content(
        """
        <input id="phone"><input id="other">
        <script>
          const phone = document.querySelector('#phone');
          phone.addEventListener('input', () => {
          phone.value = '+' + phone.value.replace(/\\D/g, '');
          });
        </script>
        """
    )
    await human_type(chromium_page, "#phone", "79161234567", value_mode="digits")
    assert await chromium_page.locator("#phone").input_value() == "+79161234567"

    await chromium_page.set_content(
        """
        <input id="target"><input id="other">
        <script>
          target.addEventListener('input', () => { if (target.value.length === 1) other.focus(); });
        </script>
        """
    )
    with pytest.raises(HumanizationError, match="type:focus_lost"):
        await human_type(chromium_page, "#target", "abc")
    assert await chromium_page.locator("#target").input_value() == "a"


@pytest.mark.asyncio
async def test_otp_replaces_old_values_and_does_not_depend_on_autofocus(chromium_page, monkeypatch):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    await chromium_page.set_content('<input id="single" maxlength="6" value="987654">')
    await human_type_digits(chromium_page, "#single", "123456")
    assert await chromium_page.locator("#single").input_value() == "123456"

    await chromium_page.set_content(
        """<div id="otp">
          <input maxlength="1" value="9"><input maxlength="1" value="9"><input maxlength="1" value="9">
          <input maxlength="1" value="9"><input maxlength="1" value="9"><input maxlength="1" value="9">
        </div>"""
    )
    cells = await chromium_page.locator("#otp input").all()
    await human_type_digits(chromium_page, cells, "654321")
    assert [await cell.input_value() for cell in cells] == list("654321")


@pytest.mark.asyncio
async def test_scroll_reports_real_document_and_container_displacement(chromium_page, monkeypatch):
    monkeypatch.setattr(humanization.random, "uniform", lambda _low, _high: 0)
    await chromium_page.set_content('<div style="height:3000px">long document</div>')
    document_result = await human_scroll(chromium_page, steps=1)
    assert document_result.moved

    await chromium_page.set_content(
        """
        <div id="panel" style="height:100px;overflow:auto"><div style="height:1000px"></div></div>
        <div id="blocked" style="height:100px;overflow:auto"><div style="height:1000px"></div></div>
        <script>blocked.addEventListener('wheel', event => event.preventDefault());</script>
        """
    )
    panel_result = await human_scroll(chromium_page, steps=1, container=chromium_page.locator("#panel"))
    assert panel_result.moved
    with pytest.raises(HumanizationError, match="scroll:scroll_not_confirmed"):
        await human_scroll(chromium_page, steps=1, container=chromium_page.locator("#blocked"))


@pytest.mark.asyncio
async def test_cancelled_interaction_releases_its_page_lock(chromium_page):
    await chromium_page.set_content('<textarea id="value"></textarea>')
    task = asyncio.create_task(human_type(chromium_page, "#value", "x" * 200))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await human_type(chromium_page, "#value", "ok", timeout_ms=2_000)
    assert await chromium_page.locator("#value").input_value() == "ok"
