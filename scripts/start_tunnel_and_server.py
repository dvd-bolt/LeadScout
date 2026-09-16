"""Launch Cloudflare Quick Tunnel and LeadScout server together."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import shutil
import threading

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
ENV_FILE = BASE_DIR / ".env"
CLOUDFLARED_PATH = shutil.which("cloudflared") or r"C:\Program Files (x86)\cloudflared\cloudflared.exe"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [Launcher]: %(message)s")
logger = logging.getLogger("launcher")


def update_env(updates: dict[str, str]) -> None:
    if not ENV_FILE.exists():
        return
    text = ENV_FILE.read_text(encoding="utf-8")
    for key, val in updates.items():
        pattern = rf"^{re.escape(key)}=.*$"
        if re.search(pattern, text, flags=re.MULTILINE):
            text = re.sub(pattern, f"{key}={val}", text, flags=re.MULTILINE)
        else:
            text = text.rstrip() + f"\n{key}={val}\n"
    ENV_FILE.write_text(text, encoding="utf-8")


async def main_async() -> None:
    logger.info("Starting Cloudflare HTTPS tunnel...")
    tunnel_proc = subprocess.Popen(
        [CLOUDFLARED_PATH, "tunnel", "--url", "http://127.0.0.1:8000", "--protocol", "http2", "--no-autoupdate"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )

    pattern = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
    app_url = None
    start = time.time()
    while time.time() - start < 35:
        line = tunnel_proc.stdout.readline()
        if not line:
            await asyncio.sleep(0.5)
            continue
        logger.info("[cloudflared] %s", line.strip())
        match = pattern.search(line)
        if match:
            app_url = match.group(0)
            break

    if not app_url:
        logger.error("Failed to detect Cloudflare tunnel URL within timeout")
        if tunnel_proc.poll() is None:
            tunnel_proc.terminate()
        sys.exit(1)

    domain = app_url.replace("https://", "").rstrip("/")
    logger.info("Detected public tunnel URL: %s", app_url)

    # Start background thread to drain cloudflared stdout so pipe doesn't block
    def _drain_tunnel_stdout(proc):
        try:
            for out_line in iter(proc.stdout.readline, ""):
                if not out_line:
                    break
        except Exception:
            pass

    threading.Thread(target=_drain_tunnel_stdout, args=(tunnel_proc,), daemon=True).start()

    origins_str = f"{app_url},http://127.0.0.1:8000,http://localhost:8000"
    update_env({
        "APP_URL": app_url,
        "DOMAIN": domain,
        "WEB_APP_ORIGINS": origins_str,
    })

    os.environ["APP_URL"] = app_url
    os.environ["DOMAIN"] = domain
    os.environ["WEB_APP_ORIGINS"] = origins_str

    import leadscout.core.config as config
    config.APP_URL = app_url
    config.WEB_APP_ORIGINS = tuple(origin.strip().rstrip("/") for origin in origins_str.split(",") if origin.strip())

    logger.info("Configuring Telegram bot Mini App menu button...")
    try:
        from scripts.set_mini_app_menu import configure_menu
        await configure_menu()
    except Exception as exc:
        logger.warning("Could not set Telegram menu button: %s", exc)

    logger.info("Starting LeadScout server...")
    try:
        from server_app import serve
        await serve()
    finally:
        logger.info("Shutting down tunnel...")
        if tunnel_proc.poll() is None:
            tunnel_proc.terminate()
            try:
                tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel_proc.kill()


if __name__ == "__main__":
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Launcher stopped by user")
