"""Local development runner: long polling instead of a webhook. `python -m app.run_polling`."""

import asyncio
import logging
import sys
from pathlib import Path

import httpx

from . import telegram
from .config import settings
from .db import init_db
from .handler import handle_update


async def main() -> None:
    # stdout + own log file: no shell redirection needed (PowerShell flags redirected stderr as NativeCommandError)
    Path("data").mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler("data/polling.log", encoding="utf-8")],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not settings.telegram_token or not settings.owner_id:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and OWNER_TELEGRAM_ID in .env")
    # Polling and a webhook exclude each other: starting here would silently disconnect the cloud bot.
    hook = (await telegram.webhook_info()).get("url")
    if hook and "--force" not in sys.argv:
        raise SystemExit(f"The cloud bot is live (webhook {hook}). Not starting locally.
"
                         "To take over anyway: python -m app.run_polling --force "
                         "(afterwards re-run scripts.set_webhook to hand back to the cloud).")
    init_db()
    await telegram.delete_webhook()
    me = await telegram.call("getMe")
    logging.info("Bot @%s running (model %s, mode %s). Ctrl+C to stop.", me["username"], settings.llm_model, settings.handler_mode)
    offset = None
    delay = 3
    while True:
        try:
            updates = await telegram.get_updates(offset)
        except httpx.TransportError as e:
            # network drops (sleep, Wi-Fi): one line, backoff up to a minute instead of a traceback every 3 s
            logging.warning("getUpdates network error (%s); retrying in %ss", type(e).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
            continue
        except Exception:
            logging.exception("getUpdates failed; retrying")
            await asyncio.sleep(3)
            continue
        delay = 3
        for update in updates:
            offset = update["update_id"] + 1
            await handle_update(update)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
