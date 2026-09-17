"""Verified, human-paced browser interactions used by the hh.ru parsers."""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from patchright.async_api import Locator, Page

DEFAULT_ACTION_TIMEOUT_MS = 10_000
DEFAULT_TRANSITION_TIMEOUT_MS = 20_000


class HumanizationError(RuntimeError):
    """A browser action could not be completed or verified safely."""

    def __init__(self, operation: str, reason: str) -> None:
        self.operation = operation
        self.reason = reason
        # Never include a field's value: it can be an OTP, phone number or letter.
        super().__init__(f"{operation}:{reason}")


@dataclass(frozen=True)
class ScrollResult:
    delta_y: float
    reached_end: bool

    @property
    def moved(self) -> bool:
        return self.delta_y != 0


@dataclass
class _PageInteractionState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    position: tuple[float, float] | None = None
    url: str | None = None


_PAGE_STATES: weakref.WeakKeyDictionary[Page, _PageInteractionState] = weakref.WeakKeyDictionary()


def generate_bezier_curve(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    num_steps: int = 25,
) -> list[tuple[float, float]]:
    """Build an eased cubic Bezier path without performing any action."""
    dx = end_x - start_x
    dy = end_y - start_y
    distance = math.hypot(dx, dy)
    if distance < 1.0 or num_steps < 2:
        return [(end_x, end_y)]

    deviation = min(distance * 0.3, 100.0)
    control_1 = (
        start_x + dx * random.uniform(0.2, 0.4) + random.uniform(-deviation, deviation),
        start_y + dy * random.uniform(0.2, 0.4) + random.uniform(-deviation, deviation),
    )
    control_2 = (
        start_x + dx * random.uniform(0.6, 0.8) + random.uniform(-deviation, deviation),
        start_y + dy * random.uniform(0.6, 0.8) + random.uniform(-deviation, deviation),
    )

    points: list[tuple[float, float]] = []
    for index in range(1, num_steps + 1):
        linear_t = index / num_steps
        t = math.sin(linear_t * math.pi / 2.0)
        inverse_t = 1.0 - t
        x = (
            inverse_t**3 * start_x
            + 3 * inverse_t**2 * t * control_1[0]
            + 3 * inverse_t * t**2 * control_2[0]
            + t**3 * end_x
        )
        y = (
            inverse_t**3 * start_y
            + 3 * inverse_t**2 * t * control_1[1]
            + 3 * inverse_t * t**2 * control_2[1]
            + t**3 * end_y
        )
        points.append((round(x, 2), round(y, 2)))
    return points


def _state_for(page: Page) -> _PageInteractionState:
    state = _PAGE_STATES.get(page)
    if state is None:
        state = _PageInteractionState(url=page.url)
        _PAGE_STATES[page] = state
    if state.url != page.url:
        state.position = None
        state.url = page.url
    return state


def _deadline(timeout_ms: int | None, *, default: int = DEFAULT_ACTION_TIMEOUT_MS) -> float:
    if timeout_ms is not None and timeout_ms <= 0:
        raise HumanizationError("interaction", "invalid_timeout")
    return time.monotonic() + (timeout_ms if timeout_ms is not None else default) / 1000


def _remaining_ms(deadline: float, operation: str) -> int:
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise HumanizationError(operation, "timeout")
    return remaining


def _locator(page: Page, selector_or_locator: str | Locator) -> Locator:
    if isinstance(selector_or_locator, str):
        return page.locator(selector_or_locator).first
    if not isinstance(selector_or_locator, Locator):
        raise HumanizationError("interaction", "invalid_target")
    return selector_or_locator


async def _wait_visible(locator: Locator, deadline: float, operation: str) -> None:
    try:
        await locator.wait_for(state="visible", timeout=_remaining_ms(deadline, operation))
    except HumanizationError:
        raise
    except Exception as exc:
        raise HumanizationError(operation, "target_not_ready") from exc


async def _prepare_editable(locator: Locator, deadline: float, operation: str) -> None:
    await _wait_visible(locator, deadline, operation)
    try:
        await locator.scroll_into_view_if_needed(timeout=_remaining_ms(deadline, operation))
        if not await locator.is_editable():
            raise HumanizationError(operation, "target_not_editable")
    except HumanizationError:
        raise
    except Exception as exc:
        raise HumanizationError(operation, "target_not_editable") from exc


async def _is_focused(locator: Locator, operation: str) -> bool:
    try:
        return bool(await locator.evaluate("element => document.activeElement === element"))
    except Exception as exc:
        raise HumanizationError(operation, "target_detached") from exc


def _normalised_value(value: str) -> str:
    # Browsers normalise CRLF in textarea values. No other normalisation is safe.
    return value.replace("\r\n", "\n")


def _matches_value(actual: str, expected: str, value_mode: Literal["exact", "digits"]) -> bool:
    if value_mode == "exact":
        return _normalised_value(actual) == _normalised_value(expected)
    return re.sub(r"\D", "", actual) == re.sub(r"\D", "", expected)


async def _clear(locator: Locator, deadline: float, operation: str) -> None:
    try:
        await locator.fill("", timeout=_remaining_ms(deadline, operation))
        if await locator.input_value(timeout=_remaining_ms(deadline, operation)):
            raise HumanizationError(operation, "clear_not_confirmed")
    except HumanizationError:
        raise
    except Exception as exc:
        raise HumanizationError(operation, "clear_failed") from exc


async def _type_characters(
    locator: Locator,
    text: str,
    deadline: float,
    *,
    operation: str,
    delay_range: tuple[float, float],
) -> None:
    try:
        await locator.focus(timeout=_remaining_ms(deadline, operation))
        # One browser RPC per character makes ordinary cover letters needlessly
        # slow. Patchright still emits keyboard/input events sequentially inside
        # each bounded chunk; the focus check between chunks preserves the
        # safety guarantee when a reactive form moves focus elsewhere.
        chunk_size = 32
        for offset in range(0, len(text), chunk_size):
            if not await _is_focused(locator, operation):
                raise HumanizationError(operation, "focus_lost")
            chunk = text[offset : offset + chunk_size]
            delay_ms = max(0.0, random.uniform(*delay_range) * 1_000)
            await locator.press_sequentially(
                chunk,
                delay=delay_ms,
                timeout=_remaining_ms(deadline, operation),
            )
            if not await _is_focused(locator, operation):
                raise HumanizationError(operation, "focus_lost")
    except asyncio.CancelledError:
        raise
    except HumanizationError:
        raise
    except Exception as exc:
        raise HumanizationError(operation, "input_failed") from exc


async def _type_one(
    locator: Locator,
    text: str,
    deadline: float,
    *,
    value_mode: Literal["exact", "digits"],
    operation: str,
    delay_range: tuple[float, float],
) -> None:
    await _prepare_editable(locator, deadline, operation)
    await _clear(locator, deadline, operation)
    await _type_characters(
        locator,
        _normalised_value(text) if value_mode == "exact" else text,
        deadline,
        operation=operation,
        delay_range=delay_range,
    )
    try:
        actual = await locator.input_value(timeout=_remaining_ms(deadline, operation))
    except HumanizationError:
        raise
    except Exception as exc:
        raise HumanizationError(operation, "value_unavailable") from exc
    if not _matches_value(actual, text, value_mode):
        raise HumanizationError(operation, "value_not_confirmed")


async def _decorative_move(page: Page, locator: Locator, state: _PageInteractionState, deadline: float) -> None:
    """Move only when a prior position is known; the locator owns the final click."""
    if state.position is None:
        return
    try:
        await locator.scroll_into_view_if_needed(timeout=_remaining_ms(deadline, "click"))
        box = await locator.bounding_box(timeout=_remaining_ms(deadline, "click"))
        viewport = page.viewport_size
        if not box or not viewport:
            return
        left = max(1.0, box["x"] + box["width"] * 0.3)
        right = min(float(viewport["width"] - 1), box["x"] + box["width"] * 0.7)
        top = max(1.0, box["y"] + box["height"] * 0.3)
        bottom = min(float(viewport["height"] - 1), box["y"] + box["height"] * 0.7)
        if left > right or top > bottom:
            return
        target = (random.uniform(left, right), random.uniform(top, bottom))
        for point in generate_bezier_curve(*state.position, *target, num_steps=random.randint(15, 25)):
            await page.mouse.move(*point)
            await asyncio.sleep(random.uniform(0.005, 0.015))
        state.position = target
        await asyncio.sleep(random.uniform(0.15, 0.35))
    except asyncio.CancelledError:
        raise
    except HumanizationError:
        raise
    except Exception:
        # The actionability-checked click below is the authority for correctness.
        state.position = None


async def human_click(page: Page, selector_or_locator: str | Locator, *, timeout_ms: int | None = None) -> None:
    """Move decoratively, then perform one actionability-checked locator click."""
    locator = _locator(page, selector_or_locator)
    deadline = _deadline(timeout_ms)
    state = _state_for(page)
    async with state.lock:
        _state_for(page)
        await _wait_visible(locator, deadline, "click")
        await _decorative_move(page, locator, state, deadline)
        try:
            await locator.click(timeout=_remaining_ms(deadline, "click"))
        except HumanizationError:
            raise
        except Exception as exc:
            raise HumanizationError("click", "action_not_confirmed") from exc
        # Save a fresh page-local start point only when this click did not
        # navigate. A later locator click still revalidates its own target.
        if state.url == page.url:
            try:
                if await locator.count() == 0 or not await locator.is_visible():
                    state.position = None
                    state.url = page.url
                    return
                box = await locator.bounding_box(
                    timeout=min(250, _remaining_ms(deadline, "click"))
                )
                viewport = page.viewport_size
                if box and viewport:
                    state.position = (
                        min(max(1.0, box["x"] + box["width"] / 2), viewport["width"] - 1),
                        min(max(1.0, box["y"] + box["height"] / 2), viewport["height"] - 1),
                    )
                else:
                    state.position = None
            except Exception:
                state.position = None
        else:
            state.position = None
        state.url = page.url


async def human_type(
    page: Page,
    selector_or_locator: str | Locator,
    text: str,
    *,
    timeout_ms: int | None = None,
    value_mode: Literal["exact", "digits"] = "exact",
) -> None:
    """Clear and type text character by character, then verify the field value."""
    if value_mode not in {"exact", "digits"}:
        raise HumanizationError("type", "invalid_value_mode")
    if not isinstance(text, str):
        raise HumanizationError("type", "invalid_text")
    deadline = _deadline(timeout_ms, default=DEFAULT_ACTION_TIMEOUT_MS + 300 * len(text))
    locator = _locator(page, selector_or_locator)
    state = _state_for(page)
    async with state.lock:
        _state_for(page)
        await _type_one(
            locator,
            text,
            deadline,
            value_mode=value_mode,
            operation="type",
            delay_range=(0.04, 0.10),
        )
        state.position = None
        state.url = page.url


async def human_type_digits(
    page: Page,
    selector_or_locators: str | Locator | Sequence[Locator],
    code: str,
    *,
    timeout_ms: int | None = None,
) -> None:
    """Type an OTP into one field or a known, ordered set of OTP cells."""
    if not isinstance(code, str) or not code.isdigit() or not code:
        raise HumanizationError("otp", "invalid_code")
    deadline = _deadline(timeout_ms, default=DEFAULT_ACTION_TIMEOUT_MS + 300 * len(code))
    if isinstance(selector_or_locators, Sequence) and not isinstance(selector_or_locators, str):
        locators = list(selector_or_locators)
        if len(locators) != len(code) or not all(isinstance(item, Locator) for item in locators):
            raise HumanizationError("otp", "ambiguous_fields")
    else:
        locators = [_locator(page, selector_or_locators)]

    state = _state_for(page)
    async with state.lock:
        _state_for(page)
        if len(locators) == 1:
            await _type_one(
                locators[0],
                code,
                deadline,
                value_mode="digits",
                operation="otp",
                delay_range=(0.12, 0.25),
            )
        else:
            for locator, digit in zip(locators, code, strict=True):
                await _type_one(
                    locator,
                    digit,
                    deadline,
                    value_mode="digits",
                    operation="otp",
                    delay_range=(0.12, 0.25),
                )
            try:
                values = []
                for locator in locators:
                    values.append(await locator.input_value(timeout=_remaining_ms(deadline, "otp")))
                actual = "".join(values)
            except HumanizationError:
                raise
            except Exception as exc:
                raise HumanizationError("otp", "value_unavailable") from exc
            if not _matches_value(actual, code, "digits"):
                raise HumanizationError("otp", "value_not_confirmed")
        state.position = None
        state.url = page.url


async def human_scroll(
    page: Page,
    steps: int | None = None,
    *,
    container: Locator | None = None,
    timeout_ms: int | None = None,
) -> ScrollResult:
    """Scroll a document or explicit container and verify its displacement."""
    deadline = _deadline(timeout_ms)
    state = _state_for(page)
    async with state.lock:
        _state_for(page)
        steps = steps if steps is not None else random.randint(3, 6)
        if steps < 1:
            raise HumanizationError("scroll", "invalid_steps")
        if container is None:
            before = await page.evaluate("() => window.scrollY")
            for _ in range(steps):
                _remaining_ms(deadline, "scroll")
                await page.mouse.wheel(0, random.randint(150, 400))
                await asyncio.sleep(random.uniform(0.4, 1.2))
                await page.evaluate("() => new Promise(requestAnimationFrame)")
            after, at_end = await page.evaluate(
                "() => [window.scrollY, window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 1]"
            )
        else:
            await _wait_visible(container, deadline, "scroll")
            before = await container.evaluate("element => element.scrollTop")
            try:
                await container.hover(timeout=_remaining_ms(deadline, "scroll"))
            except Exception as exc:
                raise HumanizationError("scroll", "container_not_ready") from exc
            state.position = None
            for _ in range(steps):
                _remaining_ms(deadline, "scroll")
                await page.mouse.wheel(0, random.randint(150, 400))
                await asyncio.sleep(random.uniform(0.4, 1.2))
                await page.evaluate("() => new Promise(requestAnimationFrame)")
            after, at_end = await container.evaluate(
                "element => [element.scrollTop, element.scrollTop + element.clientHeight >= element.scrollHeight - 1]"
            )
        delta = float(after) - float(before)
        if delta == 0 and not at_end:
            raise HumanizationError("scroll", "scroll_not_confirmed")
        return ScrollResult(delta_y=delta, reached_end=bool(at_end))
