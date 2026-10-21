"""Verifies .env: Telegram token, owner id, Anthropic and Gemini keys (one tiny call each).

    python -m scripts.check_setup
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm, telegram  # noqa: E402
from app.config import settings  # noqa: E402

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


async def main() -> None:
    try:
        me = await telegram.call("getMe")
        print(f"[ok]   Telegram bot: @{me['username']}")
    except Exception as e:
        print(f"[FAIL] Telegram token: {e}")
    print(f"[ok]   Owner id: {settings.owner_id}" if settings.owner_id else "[FAIL] OWNER_TELEGRAM_ID is empty")
    for model in ("claude-sonnet-5", "gemini-3.8-flash"):
        try:
            await llm.structured(stable_system="Answer with ok=true.", user="ping", schema=SCHEMA,
                                 effort="low", max_tokens=1000, purpose="check", model=model)
            print(f"[ok]   {model}")
        except Exception as e:
            print(f"[FAIL] {model}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
