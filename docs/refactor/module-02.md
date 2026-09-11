# LeadScout module 02 — business logic

Protocol: parallel-v1
Status: READY

## Result

The business layer is ready for module 04 integration. Mini App, bot, and
scheduler can construct the same service graph, while the legacy entry points
continue to use the canonical implementations and process-wide resources.

No API, frontend, storage implementation, lifecycle, scheduler, or application
entry-point file was changed by module 02. No external hh.ru, Telegram, or
Gemini action was executed.

## Business-service contract

```python
from leadscout.services import AuditSource, ServiceError, build_services

services = build_services(
    db=database,
    coordinator=worker.task_coordinator,
    login_manager=HHLoginManager,
    resume_manager=HHResumeManager,
    ai=None,
)
```

`build_services(*, db, coordinator, login_manager, resume_manager, ai=None)`
returns one `Services` object with these asynchronous groups and methods:

- `accounts.start_login(user_id, login, account_name="") -> dict`
- `accounts.update_settings(user_id, account_id, values) -> dict`
- `accounts.delete(user_id, account_id) -> bool`
- `resumes.sync(user_id, account_id) -> dict`
- `resumes.import_pdf(user_id, account_id, path, structured=None) -> dict`
- `resumes.delete(user_id, account_id, snapshot_id) -> dict`
- `automation.start(user_id, account_id) -> dict`
- `automation.start_all(user_id) -> dict`
- `automation.stop_all(user_id) -> dict`
- `questionnaires.edit(user_id, apply_id, cover_letter=None, answers=None) -> dict`
- `questionnaires.confirm(user_id, apply_id) -> dict`
- `questionnaires.skip(user_id, apply_id) -> dict`
- `audits.prepare(user_id, account_id=None, resume_snapshot_id=None, resume_text=None) -> AuditSource`
- `audits.run(source) -> dict`
- `audits.match(user_id, audit_id, vacancy_text=None, vacancy_url=None) -> dict`

`AuditSource` is an immutable dataclass with `user_id`, `account_id`,
`resume_snapshot_id`, and the frozen `resume_text`. `ServiceError` contains the
transport-independent `code` and `message`; supported codes are `NOT_FOUND`,
`CONFLICT`, `INVALID_INPUT`, and `LIMIT_REACHED`.

The facade uses only the injected stable database interface and contains no SQL.
The now-available atomic `database.skip_pending_questionnaire` contract is used
directly. Resume import preserves `NEEDS_FIELDS`, `missing_fields`, and
`structured` for the operation adapter. Questionnaire rows are returned in the
storage format with JSON columns intact.

## Implemented behavior

- Repeated login resolves accounts by normalized login, handles a concurrent
  duplicate-create race, stops account automation before authorization, and
  preserves `account_id` plus OTP/CAPTCHA results.
- Automation applies one ownership/session/resume/text/daily-limit validation,
  delegates deduplication to the coordinator, and returns a result for every
  account during mass start.
- Questionnaire editing validates required fields, answer types, duplicates,
  non-empty values, and allowed radio/checkbox options. Confirmation reuses a
  live submission; skip uses the atomic storage state transition.
- Audit preparation freezes the exact selected text. Audit execution preserves
  the existing weighted score formula and does not persist AI failures or
  non-profession results as successful zero-score audits.
- Vacancy matching always uses the audit's source document and accepts exactly
  one of text or a normalized HTTPS hh.ru URL. URL loading uses the audit's
  account, or the owner's active account when the audit has no account link, and
  instructs the caller to paste text when no usable session exists.
- PDF parsing and report rendering live under `leadscout.documents`; blocking
  PDF parsing in the async import workflow runs through `asyncio.to_thread`.
- Relevance is checked before the first response click. Existing selectors and
  the order of hh.ru browser actions are preserved.
- Cancellation cleans registries and browser resources. Interrupted
  questionnaire submissions are restored to `NEEDS_REVIEW`. A notification
  failure after a confirmed response does not roll back `SUBMITTED`/`SUCCESS`.

## Structure

```text
leadscout/
  models/
    questions.py
    resumes.py
    audits.py
  services/
    facade.py
  integrations/
    browser.py
    browser_pool.py
    login.py
    resumes.py
    vacancies.py
    application_forms.py
    ai/
      cache.py
      client.py
      prompts.py
      operations.py
  documents/
    pdf_reader.py
    audit_report.py
  notifications/
    base.py
    formatters.py
    telegram.py
  jobs/
    common.py
    search.py
    questionnaire.py
  runtime/
    coordinator.py
    locks.py
worker.py                  # compatibility facade
ai_handler.py              # compatibility facade
parsers/hh_*.py            # canonical module aliases
utils/pdf_generator.py     # compatibility facade
tests/parallel_services/
  test_facade.py
```

## Preserved exports and shared resources

- `worker.TaskCoordinator` is `leadscout.runtime.coordinator.TaskCoordinator`.
- `worker.task_coordinator` is the single coordinator instance.
- Legacy `start_account`, `stop_account`, `start_questionnaire`, `is_running`,
  `configure_bot`, and `shutdown` remain thin wrappers. `configure_notifier`
  accepts a custom or recording notifier for tests.
- `parsers.hh_browser`, `hh_login`, `hh_resume`, and `hh_applicant` are module
  aliases to the canonical integrations, so legacy monkeypatches affect the
  actual implementation.
- `HHLoginManager`, `HHResumeManager`, and `SharedBrowserPool` retain their
  public identities. `HHResumeManager.fetch_vacancy_text` is the same canonical
  function exposed by the vacancy integration.
- `account_locks` is the existing `utils.concurrency.account_locks` registry.
  The runtime and all browser engines reference the one canonical browser
  context semaphore and one `SharedBrowserPool` engine registry.
- `ai_handler` re-exports all existing public AI models and functions. Its
  `gemini_service` and `_AI_CACHE` are identical to the canonical AI package
  instances; `FullStructuredResume is StructuredResume`.
- `utils.pdf_generator.generate_resume_audit_pdf` is the canonical document
  report function.

## Local verification

The final permitted targeted run completed after the last source change:

```text
.venv/bin/python -m pytest -q \
  tests/parallel_services \
  tests/test_ai_and_validation.py \
  tests/test_tasks_and_browser.py \
  tests/test_humanization.py \
  tests/test_humanization_forms.py \
  tests/test_humanization_login.py \
  tests/test_pdf.py

41 passed in 80.41s
```

Scoped Ruff returned `All checks passed!`. Scoped `compileall`, compatibility
identity assertions, singleton/resource identity assertions, off-event-loop PDF
verification, notification-failure verification, and `git diff --check` also
passed.

The common test suite was intentionally not run during the parallel phase.
`READY` means the module and its contract tests are ready for integration; it is
not a claim that the eventual combined application suite has passed.

## Cross-module handoff

- `requests/02-to-01.md`: resolved; module 01 exposed the agreed atomic skip.
- `requests/02-to-04.md`: API/bot integration notes and operation-result
  adapters for module 04.
