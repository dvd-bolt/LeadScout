"""Entity-specific SQLite repositories.

Every public repository function receives a :class:`Database` as its first
argument, which keeps production and test database selection explicit.
"""

from leadscout.storage.repositories import (
    accounts,
    applications,
    audits,
    operations,
    questionnaires,
    resumes,
    users,
)

__all__ = [
    "accounts",
    "applications",
    "audits",
    "operations",
    "questionnaires",
    "resumes",
    "users",
]
