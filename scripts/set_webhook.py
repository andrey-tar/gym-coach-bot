"""Points Telegram at the cloud bot (or shows / removes the current webhook).

    .venv\\Scripts\\python -m scripts.set_webhook https://<service-url>   # register <url>/telegram
    .venv\\Scripts\\python -m scripts.set_webhook --info
    .venv\\Scripts\\python -m scripts.set_webhook --delete                # back to local polling

Uses TELEGRAM_BOT_TOKEN and WEBHOOK_SECRET from .env (the same secret the cloud service was given).
"""

import asyncio
import json
import sys

from app import telegram
from app.config import settings


async def main(args: list[str]) -> None:
    if not args or args[0] == "--info":
        print(json.dumps(await telegram.webhook_info(), ensure_ascii=False, indent=1))
    elif args[0] == "--delete":
        await telegram.delete_webhook()
        print("Webhook removed: Telegram now waits for the local bot (python -m app.run_polling).")
    else:
        if not settings.webhook_secret:
            sys.exit("Set WEBHOOK_SECRET in .env first")
        url = args[0].rstrip("/") + "/telegram"
        await telegram.set_webhook(url, settings.webhook_secret)
        print(f"Webhook set: {url}")
        print(json.dumps(await telegram.webhook_info(), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
