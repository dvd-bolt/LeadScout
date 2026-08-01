"""
LeadScout AI — Модуль симуляции человеческого поведения (Humanization Engine).
Эмулирует посимвольный ввод текста, движения мыши по физическим кубическим кривым Безье с эмуляцией замедления и естественный скроллинг страницы.
"""

import asyncio
import random
import math
import logging
from patchright.async_api import Page

logger = logging.getLogger(__name__)


def generate_bezier_curve(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    num_steps: int = 25
) -> list[tuple[float, float]]:
    """
    Генерирует траекторию движения мыши по кубической кривой Безье
    с физическим замедлением при приближении к цели.
    """
    # Вычисляем смещение для случайных контрольных точек P1 и P2
    dx = end_x - start_x
    dy = end_y - start_y
    dist = math.hypot(dx, dy)

    if dist < 1.0 or num_steps < 2:
        return [(end_x, end_y)]

    # Контрольные точки со случайными отклонениями
    deviation_scale = min(dist * 0.3, 100.0)
    
    ctrl1_x = start_x + dx * random.uniform(0.2, 0.4) + random.uniform(-deviation_scale, deviation_scale)
    ctrl1_y = start_y + dy * random.uniform(0.2, 0.4) + random.uniform(-deviation_scale, deviation_scale)

    ctrl2_x = start_x + dx * random.uniform(0.6, 0.8) + random.uniform(-deviation_scale, deviation_scale)
    ctrl2_y = start_y + dy * random.uniform(0.6, 0.8) + random.uniform(-deviation_scale, deviation_scale)

    points = []
    for i in range(1, num_steps + 1):
        # Использование синусоидального замедления t' = sin(t * pi / 2) для физической микропаузы у цели
        linear_t = i / num_steps
        t = math.sin(linear_t * math.pi / 2.0)

        one_minus_t = 1.0 - t
        x = (
            (one_minus_t ** 3) * start_x
            + 3 * (one_minus_t ** 2) * t * ctrl1_x
            + 3 * one_minus_t * (t ** 2) * ctrl2_x
            + (t ** 3) * end_x
        )
        y = (
            (one_minus_t ** 3) * start_y
            + 3 * (one_minus_t ** 2) * t * ctrl1_y
            + 3 * one_minus_t * (t ** 2) * ctrl2_y
            + (t ** 3) * end_y
        )
        points.append((round(x, 2), round(y, 2)))

    return points


LAST_MOUSE_POS: dict[str, float] = {"x": 400.0, "y": 300.0}


async def human_type(page: Page, selector_or_locator, text: str) -> None:
    """
    Человеческий ввод текста с гарантированной поддержкой кириллицы и задержками.
    Поддерживает как селектор-строку, так и объект Locator.
    """
    if isinstance(selector_or_locator, str):
        element = page.locator(selector_or_locator).first
    else:
        element = selector_or_locator

    if await element.count() > 0:
        await element.scroll_into_view_if_needed()
        await element.click()
        await asyncio.sleep(random.uniform(0.1, 0.3))
        # Гарантированное сохранение кириллического Unicode текста
        await element.fill(text)
        await asyncio.sleep(random.uniform(0.2, 0.4))


async def human_scroll(page: Page, steps: int | None = None) -> None:
    """Естественный скроллинг страницы с использованием колеса мыши."""
    if steps is None:
        steps = random.randint(3, 6)

    for _ in range(steps):
        scroll_y = random.randint(150, 400)
        await page.mouse.wheel(0, scroll_y)
        await asyncio.sleep(random.uniform(0.4, 1.2))


async def human_type_digits(page: Page, selector_or_locator, code: str) -> None:
    """
    Посимвольный ввод цифровых кодов (например, СМС OTP 4-6 знаков)
    с индивидуальными паузами (120-250 мс) между нажатиями.
    """
    if isinstance(selector_or_locator, str):
        element = page.locator(selector_or_locator).first
    else:
        element = selector_or_locator

    if await element.count() > 0:
        await element.scroll_into_view_if_needed()
        await element.click()
        await asyncio.sleep(random.uniform(0.2, 0.4))
        for digit in code:
            await page.keyboard.type(digit)
            await asyncio.sleep(random.uniform(0.12, 0.25))


async def human_click(page: Page, selector_or_locator) -> None:
    """
    Плавное наведение на элемент по кривой Безье с микропаузой и кликом.
    Поддерживает как селектор-строку, так и объект Locator.
    """
    global LAST_MOUSE_POS

    if isinstance(selector_or_locator, str):
        element = page.locator(selector_or_locator).first
    else:
        element = selector_or_locator

    if await element.count() == 0:
        return

    await element.scroll_into_view_if_needed()
    await asyncio.sleep(random.uniform(0.1, 0.3))

    box = await element.bounding_box()
    if box:
        # Целевая точка со случайным смещением в пределах кнопки
        target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
        target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

        # Текущее непрерывное положение мыши
        start_x = LAST_MOUSE_POS["x"]
        start_y = LAST_MOUSE_POS["y"]

        # Генерация и прохождение кривой Безье
        curve_points = generate_bezier_curve(start_x, start_y, target_x, target_y, num_steps=random.randint(15, 25))
        for pt_x, pt_y in curve_points:
            await page.mouse.move(pt_x, pt_y)
            await asyncio.sleep(random.uniform(0.005, 0.015))

        LAST_MOUSE_POS["x"] = target_x
        LAST_MOUSE_POS["y"] = target_y

        await asyncio.sleep(random.uniform(0.15, 0.35))
        await page.mouse.click(target_x, target_y)
    else:
        await element.click()

