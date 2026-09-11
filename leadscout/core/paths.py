"""Filesystem locations shared by the LeadScout backend."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"
ASSETS_DIR = PROJECT_ROOT / "assets"
WEB_DIST_DIR = PROJECT_ROOT / "web" / "dist"
DEFAULT_DB_PATH = PROJECT_ROOT / "leadscout.db"
