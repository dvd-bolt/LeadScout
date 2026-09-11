"""Built React UI in real Chromium with contract-level HTTP interception only."""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import pytest_asyncio
from patchright.async_api import Route, async_playwright, expect

PROJECT_ROOT = Path(__file__).resolve().parents[2]
WEB_DIST = PROJECT_ROOT / "web" / "dist"


def account(account_id: int, name: str, *, ready: bool = True) -> dict:
    return {
        "id": account_id,
        "account_name": name,
        "session_status": "ACTIVE" if ready else "EXPIRED",
        "active_resume_hh_id": f"resume-{account_id}" if ready else "",
        "active_resume_title": f"Резюме {name}" if ready else "",
        "daily_limit": 50,
        "applied_today": account_id,
        "applied_date": "2026-09-11",
        "auto_apply_enabled": False,
        "automation_state": "WAITING" if ready else "NEEDS_LOGIN",
        "only_remote": False,
        "send_cover_letter": True,
        "min_salary": 100000,
        "keywords": f"keywords-{account_id}",
        "stop_words": "",
        "has_proxy": False,
        "last_synced_at": "2026-09-11 08:00:00",
        "next_scheduled_search_at": "",
        "resume_ready": ready,
    }


def questionnaire(item_id: int, account_id: int, title: str, status: str = "PENDING") -> dict:
    return {
        "id": item_id,
        "account_id": account_id,
        "vacancy_url": f"https://hh.ru/vacancy/{item_id}",
        "vacancy_title": title,
        "cover_letter": f"Письмо {item_id}",
        "questions": [
            {
                "field_id": "experience",
                "label": "Есть опыт Python?",
                "answer_type": "radio",
                "required": True,
                "options": ["Да", "Нет"],
            },
        ],
        "ai_payload": {"answers": [{"field_id": "experience", "value": "Да", "answer_type": "radio"}]},
        "resume_title": f"Резюме {account_id}",
        "status": status,
        "revision": 0,
        "error_text": "",
        "updated_at": "2026-09-11 08:00:00",
    }


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        requested = Path(urlsplit(self.path).path.lstrip("/"))
        if self.path.startswith("/api/"):
            self.send_error(500, "API interception failed")
            return
        if requested and (Path(self.directory) / requested).is_file():
            super().do_GET()
            return
        self.path = "/index.html"
        super().do_GET()


@dataclass
class CapturedRequest:
    method: str
    path: str
    body: bytes
    headers: dict[str, str]


class ContractApi:
    def __init__(self):
        self.accounts = [account(1, "Основной"), account(2, "Второй", ready=False)]
        self.active_id: int | None = 1
        self.questionnaires = {
            101: questionnaire(101, 1, "Сохранить анкету"),
            102: questionnaire(102, 1, "Подтвердить анкету"),
            103: questionnaire(103, 1, "Пропустить анкету"),
            104: questionnaire(104, 1, "Конфликт анкеты"),
            202: questionnaire(202, 2, "Обработанная анкета", "SKIPPED"),
        }
        self.resumes = {
            1: [
                {
                    "snapshot_id": 11,
                    "id": "resume-1",
                    "hh_resume_id": "resume-1",
                    "title": "Python developer",
                    "href": "https://hh.ru/resume/resume-1",
                    "status": "ACTIVE",
                    "extracted_text": "Python SQL",
                    "synced_at": "2026-09-11 08:00:00",
                }
            ],
            2: [],
        }
        self.audits = [
            {
                "id": 501,
                "account_id": 1,
                "profession_name": "Backend developer",
                "overall_score": 78,
                "category_scores": {"hard_skills": 80, "impact_metrics": 65},
                "penalties": ["Мало измеримых результатов"],
                "top_recommendations": ["Добавить метрики"],
                "insights": [
                    {"tier": 1, "title": "Результаты", "description": "Уточните эффект работы", "score_impact": "+8"}
                ],
                "summary_text": "Сильная техническая база.",
                "created_at": "2026-09-11 08:00:00",
            }
        ]
        self.operations: dict[str, dict] = {}
        self.requests: list[CapturedRequest] = []
        self.import_count = 0
        self.match_count = 0
        self.skip_conflict = False
        self.captcha_version = 0

    def dashboard(self) -> dict:
        pending = sum(item["status"] in {"PENDING", "FAILED", "NEEDS_REVIEW"} for item in self.questionnaires.values())
        return {
            "user_id": 42,
            "role": "USER",
            "admin_capabilities": [],
            "csrf_token": "browser-csrf",
            "accounts": self.accounts,
            "active_account_id": self.active_id,
            "stats": {"applied": 1, "processed": 2, "errors": 0, "skipped": 1},
            "pending_review_count": pending,
            "active_pending_review_count": sum(
                item["status"] in {"PENDING", "FAILED", "NEEDS_REVIEW"} and item["account_id"] == self.active_id
                for item in self.questionnaires.values()
            ),
            "recent_events": [],
            "next_scheduled_search_at": "2026-09-11 12:00:00",
        }

    async def json_response(self, route: Route, data: object, status: int = 200):
        await route.fulfill(status=status, content_type="application/json", body=json.dumps(data, ensure_ascii=False))

    async def handle(self, route: Route):
        request = route.request
        parsed = urlsplit(request.url)
        path = parsed.path.removeprefix("/api/v1")
        body = request.post_data_buffer or b""
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.requests.append(CapturedRequest(request.method, path, body, headers))
        method = request.method

        if method != "GET" and path != "/auth/telegram":
            assert headers.get("x-csrf-token") == "browser-csrf"
        if method == "GET" and path == "/me":
            await self.json_response(route, self.dashboard())
            return
        if method == "POST" and path.startswith("/accounts/") and path.endswith("/activate"):
            target = int(path.split("/")[2])
            if not any(item["id"] == target for item in self.accounts):
                await self.json_response(route, {"detail": "Аккаунт не найден"}, 404)
                return
            self.active_id = target
            await self.json_response(route, {"active_account_id": target})
            return
        if method == "PATCH" and path.startswith("/accounts/"):
            target = int(path.split("/")[2])
            values = json.loads(body)
            current = next(item for item in self.accounts if item["id"] == target)
            current.update(values)
            await self.json_response(route, current)
            return
        if method == "POST" and path == "/automation/start-all":
            await self.json_response(
                route,
                {"results": [{"account_id": 1, "status": "STARTED"}, {"account_id": 2, "status": "NEEDS_LOGIN"}]},
                202,
            )
            return
        if method == "POST" and path == "/automation/stop-all":
            await self.json_response(route, {"status": "STOPPED", "account_ids": [1, 2]})
            return
        if method == "POST" and path.startswith("/automation/"):
            target = int(path.split("/")[2])
            current = next(item for item in self.accounts if item["id"] == target)
            running = path.endswith("/start")
            current.update(auto_apply_enabled=running, automation_state="SEARCHING" if running else "WAITING")
            await self.json_response(route, {"account_id": target, "status": "STARTED" if running else "STOPPED"}, 202)
            return
        if method == "POST" and path == "/login-flows/start":
            await self.json_response(
                route,
                {"account_id": 1, "status": "WAITING_FOR_CAPTCHA", "captcha_data_uri": self.captcha_uri("ru")},
                202,
            )
            return
        if method == "POST" and path == "/login-flows/captcha":
            await self.json_response(route, {"status": "INVALID_CAPTCHA", "message": "Неверный код с картинки."}, 202)
            return
        if method == "POST" and path == "/login-flows/captcha/reload":
            self.captcha_version += 1
            await self.json_response(
                route,
                {
                    "status": "WAITING_FOR_CAPTCHA",
                    "captcha_data_uri": self.captcha_uri(f"reload-{self.captcha_version}"),
                },
                202,
            )
            return
        if method == "POST" and path == "/login-flows/captcha/language":
            await self.json_response(
                route,
                {
                    "status": "WAITING_FOR_CAPTCHA",
                    "captcha_data_uri": self.captcha_uri("en"),
                    "message": "Язык капчи изменён",
                },
                202,
            )
            return
        if method == "POST" and path == "/login-flows/cancel":
            await route.fulfill(status=204, body="")
            return
        if method == "GET" and path.startswith("/accounts/") and path.endswith("/resumes"):
            target = int(path.split("/")[2])
            if not any(item["id"] == target for item in self.accounts):
                await self.json_response(route, {"detail": "Аккаунт не найден"}, 404)
                return
            await self.json_response(route, self.resumes.get(target, []))
            return
        if method == "POST" and path.endswith("/resumes/import"):
            self.import_count += 1
            operation_id = f"import-{self.import_count}"
            if self.import_count == 1:
                self.operations[operation_id] = {
                    "id": operation_id,
                    "kind": "resume-import",
                    "status": "NEEDS_INPUT",
                    "result": {
                        "status": "NEEDS_FIELDS",
                        "missing_fields": ["first_name", "birth_date", "city", "title"],
                        "structured": {"skills": ["Python"], "about": "Сохранённое описание"},
                        "message": "Для мастера hh.ru не хватает обязательных данных.",
                    },
                    "error_text": "",
                }
            elif self.import_count == 2:
                self.operations[operation_id] = {
                    "id": operation_id,
                    "kind": "resume-import",
                    "status": "SUCCEEDED",
                    "result": {"status": "SUCCESS", "message": "Новое резюме создано"},
                    "error_text": "",
                }
            else:
                self.operations[operation_id] = {
                    "id": operation_id,
                    "kind": "resume-import",
                    "status": "SUCCEEDED",
                    "result": {"status": "ERROR", "message": "hh.ru не подтвердил создание резюме"},
                    "error_text": "",
                }
            await self.json_response(route, {"operation_id": operation_id, "status": "PENDING"}, 202)
            return
        if method == "GET" and path.startswith("/questionnaires/"):
            item_id = int(path.rsplit("/", 1)[1])
            item = self.questionnaires.get(item_id)
            await self.json_response(route, item or {"detail": "Анкета не найдена"}, 200 if item else 404)
            return
        if method == "GET" and path == "/questionnaires":
            query = parse_qs(parsed.query)
            account_id = int(query["account_id"][0]) if "account_id" in query else None
            items = [
                item for item in self.questionnaires.values() if account_id is None or item["account_id"] == account_id
            ]
            await self.json_response(route, items)
            return
        if method == "PATCH" and path.startswith("/questionnaires/"):
            item_id = int(path.rsplit("/", 1)[1])
            self.questionnaires[item_id].update(json.loads(body))
            await self.json_response(route, self.questionnaires[item_id])
            return
        if method == "POST" and path.endswith("/confirm"):
            item_id = int(path.split("/")[2])
            self.questionnaires[item_id]["status"] = "SUBMITTING"
            await self.json_response(route, {"status": "STARTED", "apply_id": item_id}, 202)
            return
        if method == "POST" and path.endswith("/skip"):
            item_id = int(path.split("/")[2])
            if self.skip_conflict and item_id == 104:
                self.questionnaires[item_id]["status"] = "SUBMITTING"
                await self.json_response(route, {"detail": "Анкета уже отправляется"}, 409)
                return
            self.questionnaires[item_id]["status"] = "SKIPPED"
            await self.json_response(route, self.questionnaires[item_id])
            return
        if method == "GET" and path == "/applications":
            await self.json_response(
                route, {"history": [], "stats": {"applied": 0, "processed": 0, "errors": 0, "skipped": 0}}
            )
            return
        if method == "GET" and path == "/audits":
            query = parse_qs(parsed.query)
            account_id = int(query["account_id"][0]) if "account_id" in query else None
            result = [item for item in self.audits if account_id is None or item.get("account_id") == account_id]
            await self.json_response(route, result)
            return
        if method == "POST" and path == "/audits":
            operation_id = "audit-new"
            if not any(item["id"] == 502 for item in self.audits):
                self.audits.insert(
                    0,
                    {**self.audits[0], "id": 502, "account_id": self.active_id, "profession_name": "Независимый аудит"},
                )
            self.operations[operation_id] = {
                "id": operation_id,
                "kind": "resume-audit",
                "status": "SUCCEEDED",
                "result": {"audit_id": 502},
                "error_text": "",
            }
            await self.json_response(route, {"operation_id": operation_id, "status": "PENDING"}, 202)
            return
        if method == "POST" and path == "/audits/pdf":
            operation_id = "audit-pdf"
            self.operations[operation_id] = {
                "id": operation_id,
                "kind": "pdf-resume-audit",
                "status": "SUCCEEDED",
                "result": {"audit_id": 503},
                "error_text": "",
            }
            await self.json_response(route, {"operation_id": operation_id, "status": "PENDING"}, 202)
            return
        if method == "POST" and path.endswith("/match"):
            self.match_count += 1
            operation_id = f"match-{self.match_count}"
            self.operations[operation_id] = {
                "id": operation_id,
                "kind": "vacancy-match",
                "status": "SUCCEEDED",
                "result": {
                    "match_score": 84,
                    "is_suitable": True,
                    "matching_skills": ["Python"],
                    "missing_skills": ["Kafka"],
                    "advice_for_apply": "Можно откликаться",
                },
                "error_text": "",
            }
            await self.json_response(route, {"operation_id": operation_id, "status": "PENDING"}, 202)
            return
        if method == "GET" and path.startswith("/operations/"):
            operation = self.operations.get(path.rsplit("/", 1)[1])
            await self.json_response(route, operation or {"detail": "Операция не найдена"}, 200 if operation else 404)
            return
        await self.json_response(route, {"detail": f"Mock route is not implemented: {method} {path}"}, 404)

    @staticmethod
    def captcha_uri(label: str) -> str:
        svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="180" height="60"><text x="5" y="35">{label}</text></svg>'
        return "data:image/svg+xml," + svg


@pytest_asyncio.fixture
async def browser_app(tmp_path):
    if not (WEB_DIST / "index.html").exists():
        pytest.skip("Run npm --prefix web run build first")
    site = tmp_path / "site"
    shutil.copytree(WEB_DIST, site)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), lambda *args, **kwargs: QuietHandler(*args, directory=str(site), **kwargs)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    mock = ContractApi()
    runtime_errors: list[str] = []
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(tmp_path / "browser-profile"),
            headless=True,
            viewport={"width": 390, "height": 844},
        )
        await context.route("**/api/v1/**", mock.handle)
        page = context.pages[0] if context.pages else await context.new_page()
        page.on("pageerror", lambda error: runtime_errors.append(str(error)))
        try:
            yield SimpleNamespace(page=page, mock=mock, base_url=base_url, errors=runtime_errors)
            assert not runtime_errors, runtime_errors
        finally:
            await context.unroute_all(behavior="ignoreErrors")
            await context.close()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


async def test_four_pages_mobile_account_isolation_and_captcha_controls(browser_app):
    page, mock, base_url = browser_app.page, browser_app.mock, browser_app.base_url
    await page.goto(f"{base_url}/#/", wait_until="networkidle")
    await expect(page.get_by_role("heading", name="Поиск под контролем")).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), await page.evaluate(
        "Array.from(document.querySelectorAll('*')).filter(el=>el.getBoundingClientRect().right>innerWidth).map(el=>[el.tagName,el.className,el.getBoundingClientRect().right])"
    )

    await page.get_by_role("link", name="Отклики", exact=False).click()
    await expect(page.get_by_role("button", name="История")).to_be_visible()
    await page.get_by_role("link", name="Резюме", exact=True).click()
    await expect(page.get_by_role("heading", name="Резюме hh.ru")).to_be_visible()
    await page.get_by_role("link", name="Настройки", exact=True).click()
    await expect(page.get_by_role("heading", name="Настройки поиска")).to_be_visible()

    await page.get_by_label("Ключевые слова", exact=True).fill("Несохранённый черновик")
    await page.get_by_label("Активный аккаунт").select_option("2")
    await expect(page.get_by_label("Ключевые слова", exact=True)).to_have_value("keywords-2")
    await page.get_by_label("Ключевые слова", exact=True).fill("Второй аккаунт: Go")
    await page.get_by_role("button", name="Сохранить настройки").click()
    await expect(page.get_by_role("status")).to_have_text("Настройки сохранены")
    assert next(item for item in mock.accounts if item["id"] == 1)["keywords"] == "keywords-1"

    await page.get_by_role("link", name="Резюме", exact=True).click()
    await expect(page.get_by_text("Нажми «Синхронизировать», чтобы загрузить список.", exact=True)).to_be_visible()
    await page.get_by_role("link", name="Настройки", exact=True).click()

    await page.get_by_label("Телефон или email hh.ru").fill("user@example.com")
    await page.get_by_role("button", name="Продолжить").click()
    await expect(page.get_by_label("Текст с картинки")).to_be_visible()
    first_src = await page.get_by_alt_text("Капча hh.ru").get_attribute("src")
    await page.get_by_label("Текст с картинки").fill("wrong")
    await page.get_by_role("button", name="Подтвердить").click()
    await expect(page.get_by_role("status").filter(has_text="Неверный код с картинки.")).to_have_text(
        "Неверный код с картинки."
    )
    await expect(page.get_by_label("Текст с картинки")).to_be_visible()
    await page.get_by_role("button", name="Обновить капчу").click()
    await expect(page.get_by_alt_text("Капча hh.ru")).not_to_have_attribute("src", first_src or "")
    await page.get_by_role("button", name="Сменить язык").click()
    await expect(page.get_by_role("status").filter(has_text="Язык капчи изменён")).to_have_text("Язык капчи изменён")
    assert any(request.path == "/login-flows/captcha/language" for request in mock.requests)


async def test_questionnaire_save_confirm_skip_and_conflict_refresh(browser_app):
    page, mock, base_url = browser_app.page, browser_app.mock, browser_app.base_url
    await page.goto(f"{base_url}/#/applications", wait_until="networkidle")

    save_card = page.locator("article").filter(has_text="Сохранить анкету")
    await save_card.get_by_label("Сопроводительное письмо").fill("Новое сохранённое письмо")
    await save_card.get_by_role("button", name="Сохранить").click()
    await expect(page.get_by_role("status")).to_have_text("Анкета сохранена")
    assert mock.questionnaires[101]["cover_letter"] == "Новое сохранённое письмо"

    confirm_card = page.locator("article").filter(has_text="Подтвердить анкету")
    await confirm_card.get_by_role("button", name="Подтвердить отправку").click()
    await expect(page.get_by_role("status")).to_have_text("Анкета передана на отправку")
    assert mock.questionnaires[102]["status"] == "SUBMITTING"

    skip_card = page.locator("article").filter(has_text="Пропустить анкету")
    await skip_card.get_by_role("button", name="Пропустить").click()
    await expect(page.get_by_role("status")).to_have_text("Анкета пропущена")
    await expect(page.get_by_text("Пропустить анкету", exact=True)).to_be_hidden()
    assert mock.questionnaires[103]["status"] == "SKIPPED"

    mock.skip_conflict = True
    conflict_card = page.locator("article").filter(has_text="Конфликт анкеты")
    await conflict_card.get_by_role("button", name="Пропустить").click()
    await expect(page.get_by_role("status")).to_contain_text("Актуальное состояние: Отправляется")
    assert any(request.path == "/questionnaires/104" and request.method == "GET" for request in mock.requests)


async def test_partial_start_all_has_per_account_results(browser_app):
    page, base_url = browser_app.page, browser_app.base_url
    await page.goto(f"{base_url}/#/", wait_until="networkidle")
    await page.get_by_text("Все аккаунты", exact=True).click()
    await page.get_by_role("button", name="Запустить все").click()
    await expect(page.get_by_role("status")).to_have_text("Часть аккаунтов не запущена. Проверьте результаты ниже.")
    results = page.get_by_label("Результаты массового запуска")
    await expect(results).to_contain_text("Основной")
    await expect(results).to_contain_text("Запущен")
    await expect(results).to_contain_text("Второй")
    await expect(results).to_contain_text("Нужен повторный вход")


async def test_import_needs_input_has_no_auto_retry_then_succeeds_and_reports_error(browser_app, tmp_path):
    page, mock, base_url = browser_app.page, browser_app.mock, browser_app.base_url
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\nmock resume")
    await page.goto(f"{base_url}/#/resumes", wait_until="networkidle")
    await page.get_by_label("PDF для импорта в hh.ru").set_input_files(str(pdf))
    await page.get_by_role("button", name="Импортировать в hh.ru").click()
    await expect(page.get_by_role("heading", name="Дополните данные")).to_be_visible()
    await page.wait_for_timeout(500)
    assert mock.import_count == 1
    await page.get_by_label("Имя").fill("Иван")
    await page.get_by_label("Дата рождения").fill("1990-02-03")
    await page.get_by_label("Город").fill("Москва")
    await page.get_by_label("Желаемая должность").fill("Python-разработчик")
    await page.get_by_role("button", name="Подтвердить и продолжить импорт").click()
    await expect(page.get_by_role("status").filter(has_text="Новое резюме создано")).to_have_text(
        "Новое резюме создано"
    )
    assert mock.import_count == 2
    continuation = [request for request in mock.requests if request.path.endswith("/resumes/import")][1]
    multipart = continuation.body.decode("utf-8", errors="ignore")
    assert 'name="file"; filename="resume.pdf"' in multipart
    assert 'name="structured_json"' in multipart
    assert "Сохранённое описание" in multipart and "Python-разработчик" in multipart

    await page.get_by_label("PDF для импорта в hh.ru").set_input_files(str(pdf))
    await page.get_by_role("button", name="Импортировать в hh.ru").click()
    await expect(page.get_by_role("status").filter(has_text="hh.ru не подтвердил создание резюме")).to_have_text(
        "hh.ru не подтвердил создание резюме"
    )
    assert mock.import_count == 3


async def test_independent_audit_details_and_text_or_url_match_payload(browser_app):
    page, mock, base_url = browser_app.page, browser_app.mock, browser_app.base_url
    mock.accounts = []
    mock.active_id = None
    mock.audits[0]["account_id"] = None
    await page.goto(f"{base_url}/?target=audit#/", wait_until="networkidle")
    await expect(page.get_by_text("Независимый аудит доступен ниже.", exact=False)).to_be_visible()
    await expect(page.get_by_role("heading", name="ИИ-аудит")).to_be_visible()

    await page.get_by_label("Текст резюме").fill(
        "Python backend developer with SQL, APIs, tests and production delivery experience."
    )
    await page.get_by_role("button", name="Проверить текст").click()
    await expect(page.get_by_text("Независимый аудит", exact=True)).to_be_visible()
    audit_card = page.locator("article").filter(has_text="Backend developer")
    await audit_card.get_by_text("Подробный отчёт").click()
    await expect(audit_card).to_contain_text("Мало измеримых результатов")
    await expect(audit_card).to_contain_text("Добавить метрики")
    await expect(audit_card).to_contain_text("Результаты")
    await expect(audit_card).to_contain_text("+8")

    await audit_card.get_by_text("Сравнить с вакансией", exact=True).click()
    await audit_card.get_by_label("Текст вакансии", exact=True).fill(
        "Ищем Python backend разработчика со знанием Kafka"
    )
    await audit_card.get_by_role("button", name="Сравнить").click()
    await expect(audit_card).to_contain_text("84/100")
    text_match = [request for request in mock.requests if request.path.endswith("/match")][-1]
    assert set(json.loads(text_match.body)) == {"vacancy_text"}

    await audit_card.get_by_role("button", name="Ссылка hh.ru").click()
    await audit_card.get_by_label("Ссылка на вакансию hh.ru").fill("https://hh.ru/vacancy/123456")
    await audit_card.get_by_role("button", name="Сравнить").click()
    await expect(audit_card).to_contain_text("84/100")
    url_match = [request for request in mock.requests if request.path.endswith("/match")][-1]
    assert set(json.loads(url_match.body)) == {"vacancy_url"}


async def test_direct_links_validate_before_account_switch(browser_app):
    page, mock, base_url = browser_app.page, browser_app.mock, browser_app.base_url
    await page.goto(f"{base_url}/?target=questionnaire&apply_id=202#/", wait_until="networkidle")
    await expect(page.get_by_text("Обработанная анкета", exact=True)).to_be_visible()
    await expect(page.get_by_text("Пропущена", exact=True)).to_be_visible()
    await expect(page.get_by_label("Активный аккаунт")).to_have_value("2")
    assert not any(request.path.endswith("/confirm") for request in mock.requests)

    mock.active_id = 1
    await page.goto(f"{base_url}/?target=resume&account_id=999&snapshot_id=11#/", wait_until="networkidle")
    await expect(page.get_by_role("status")).to_have_text("Аккаунт для этого резюме недоступен.")
    assert mock.active_id == 1
    assert not any(request.path == "/accounts/999/activate" for request in mock.requests)

    await page.goto(f"{base_url}/?target=resume&account_id=1&snapshot_id=11#/", wait_until="networkidle")
    await expect(page.get_by_text("Python developer", exact=True)).to_be_visible()
    await expect(page.get_by_label("Активный аккаунт")).to_have_value("1")

    await page.goto(f"{base_url}/?target=accounts&account_id=2#/", wait_until="networkidle")
    await expect(page.get_by_role("heading", name="Настройки поиска")).to_be_visible()
    await expect(page.get_by_label("Активный аккаунт")).to_have_value("2")

    await page.goto(f"{base_url}/?target=applications&account_id=1&apply_id=101#/", wait_until="networkidle")
    await expect(page.get_by_text("Сохранить анкету", exact=True)).to_be_visible()
    await expect(page.get_by_text("Анкета из уведомления", exact=True)).to_be_visible()


async def test_mono_start_stop_and_account_reset(browser_app):
    page, mock, base_url = browser_app.page, browser_app.mock, browser_app.base_url
    await page.goto(f"{base_url}/#/", wait_until="networkidle")
    await page.get_by_role("button", name="Запустить поиск", exact=True).click()
    await expect(page.get_by_text("Идёт поиск", exact=True)).to_be_visible()
    await expect(page.get_by_text("Следующий запуск · МСК", exact=True)).to_be_visible()
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    await page.get_by_role("button", name="Остановить поиск", exact=True).click()
    await expect(page.get_by_text("Готов к запуску", exact=True)).to_be_visible()
    assert [r.path for r in mock.requests if r.path.startswith("/automation/")] == [
        "/automation/1/start",
        "/automation/1/stop",
    ]
    await page.get_by_text("Все аккаунты", exact=True).click()
    await page.get_by_role("button", name="Запустить все", exact=True).click()
    await expect(page.get_by_label("Результаты массового запуска")).to_be_visible()
    await page.get_by_label("Активный аккаунт").select_option("2")
    await expect(page.get_by_label("Результаты массового запуска")).to_have_count(0)
    await expect(page.get_by_role("button", name="Запустить поиск", exact=True)).to_be_disabled()
    await expect(page.get_by_role("status")).to_have_count(0)
    await page.get_by_text("Все аккаунты", exact=True).click()
    await page.get_by_role("button", name="Остановить всё", exact=True).click()
    await expect(page.get_by_role("status")).to_have_text("Все автоматизации остановлены")
    assert any(r.path == "/automation/stop-all" for r in mock.requests)
