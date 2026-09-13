"""Run the compact Telegram bot and scheduler."""

import asyncio
import logging
import os
import sys
from pathlib import Path

from leadscout.core.config import ConfigurationError, validate_runtime_config
from leadscout.runtime.context import get_default_context
from leadscout.runtime.runner import run_application


def ensure_project_venv() -> None:
    """Restart a direct Windows launch inside the project's virtual environment."""
    if sys.prefix != sys.base_prefix:
        return

    venv_python = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        print(
            "Project environment is missing. Run: py -3.13 -m venv .venv",
            file=sys.stderr,
        )
        raise SystemExit(2)

    os.execv(str(venv_python), [str(venv_python), *sys.argv])


if __name__ == "__main__":
    ensure_project_venv()

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8")

async def main(context=None):
    validate_runtime_config()
    await run_application(context if context is not None else get_default_context(), with_api=False)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except ConfigurationError as exc:
        logging.critical("Configuration error: %s", exc)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        pass
