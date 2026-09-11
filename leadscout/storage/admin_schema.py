"""Schema v7 additions; executed inside the parent migration transaction."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS access_members (
    telegram_id INTEGER PRIMARY KEY CHECK(telegram_id > 0),
    role TEXT NOT NULL CHECK(role IN ('ROOT','ADMIN','USER')),
    access_status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(access_status IN ('ACTIVE','BLOCKED')),
    display_label TEXT NOT NULL DEFAULT '' CHECK(length(display_label) <= 100),
    telegram_name TEXT NOT NULL DEFAULT '', telegram_username TEXT NOT NULL DEFAULT '',
    auth_version INTEGER NOT NULL DEFAULT 1 CHECK(auth_version >= 1), revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT, created_by INTEGER, updated_by INTEGER,
    CHECK(role <> 'ROOT' OR access_status = 'ACTIVE')
);
CREATE UNIQUE INDEX IF NOT EXISTS one_root ON access_members(role) WHERE role = 'ROOT';
CREATE TABLE IF NOT EXISTS admin_tasks (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, user_id INTEGER NOT NULL, account_id INTEGER,
    source_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('QUEUED','RUNNING','WAITING_INPUT','STOPPING','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')),
    code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT, finished_at TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS admin_tasks_owner_state ON admin_tasks(user_id, status, created_at DESC);
CREATE TABLE IF NOT EXISTS admin_actions (
    id TEXT PRIMARY KEY, actor_id INTEGER NOT NULL, kind TEXT NOT NULL, target_id INTEGER,
    request_key TEXT NOT NULL, request_hash TEXT NOT NULL,
    status TEXT NOT NULL, targets_json TEXT NOT NULL DEFAULT '[]', results_json TEXT NOT NULL DEFAULT '[]',
    changes_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(actor_id, request_key)
);
CREATE INDEX IF NOT EXISTS admin_actions_status ON admin_actions(status, created_at DESC);
CREATE TABLE IF NOT EXISTS admin_errors (
    id TEXT PRIMARY KEY, component TEXT NOT NULL, code TEXT NOT NULL,
    correlation_id TEXT NOT NULL DEFAULT '', task_id TEXT, user_id INTEGER,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS admin_errors_created ON admin_errors(created_at DESC);
"""
