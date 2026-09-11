"""Run the compact Telegram bot and scheduler."""

import asyncio
import logging

from leadscout.core.config import ConfigurationError, validate_runtime_config
from leadscout.runtime.context import get_default_context
from leadscout.runtime.runner import run_application


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
