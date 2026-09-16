"""Route groups exported for application assembly."""

from .accounts import router as accounts_router
from .admin import router as admin_router
from .applications import router as applications_router
from .audits import router as audits_router
from .auth import router as auth_router
from .automation import router as automation_router
from .captcha import router as captcha_router
from .login import router as login_router
from .operations import router as operations_router
from .questionnaires import router as questionnaires_router
from .resume_drafts import router as resume_drafts_router
from .resumes import router as resumes_router

ROUTERS = (
    admin_router,
    auth_router,
    accounts_router,
    login_router,
    resumes_router,
    resume_drafts_router,
    questionnaires_router,
    automation_router,
    captcha_router,
    applications_router,
    audits_router,
    operations_router,
)

__all__ = ["ROUTERS"]
