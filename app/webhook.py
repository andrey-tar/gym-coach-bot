"""Production entry point: Telegram webhook + weekly job endpoint (spec Sections 8, 10).

Cloud Run runs `uvicorn app.webhook:app`. Each update is processed inside its request, so the default
request-based billing (CPU only while a request is open) is enough. Deploy with max-instances=1: the
per-process lock in the handler then serializes all updates (spec 10, sequential processing).

If processing outlasts Telegram's patience it redelivers the update; the update_id dedupe in the
handler drops the copy (spec 10.1 idempotency).
"""

import hmac
import logging
import sys
from contextlib import asynccontextmanager

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from . import jobs
from .config import settings
from .db import db_session, init_db
from .handler import handle_update
from .models import Profile

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)
for _name in ("httpx", "httpx2"):  # request lines would put the bot token (in the URL) into Cloud Logging
    logging.getLogger(_name).setLevel(logging.WARNING)
log = logging.getLogger(__name__)


def _secret_ok(given: str | None, expected: str) -> bool:
    return bool(expected) and given is not None and hmac.compare_digest(given, expected)


async def telegram_update(request: Request) -> PlainTextResponse:
    # Authenticity (spec 10.1): Telegram echoes the secret_token given at setWebhook in this header.
    if not _secret_ok(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), settings.webhook_secret):
        log.warning("webhook call with a wrong secret rejected")
        return PlainTextResponse("forbidden", status_code=403)
    await handle_update(await request.json())
    return PlainTextResponse("ok")


async def weekly_job(request: Request) -> PlainTextResponse:
    if not _secret_ok(request.headers.get("X-Jobs-Secret"), settings.jobs_secret):
        log.warning("jobs call with a wrong secret rejected")
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse(await jobs.run_weekly())


async def keepalive_job(request: Request) -> PlainTextResponse:
    """Daily DB touch: Supabase pauses free projects after ~7 days of low activity (e.g. a week off training)."""
    if not _secret_ok(request.headers.get("X-Jobs-Secret"), settings.jobs_secret):
        return PlainTextResponse("forbidden", status_code=403)
    with db_session() as db:
        db.get(Profile, 1)
    return PlainTextResponse("ok")


async def health(request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@asynccontextmanager
async def lifespan(app):
    if not (settings.telegram_token and settings.owner_id and settings.webhook_secret and settings.jobs_secret):
        raise SystemExit("Set TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID, WEBHOOK_SECRET and JOBS_SECRET")
    init_db()
    log.info("Webhook app ready (model %s, mode %s)", settings.llm_model, settings.handler_mode)
    yield


app = Starlette(
    routes=[
        Route("/telegram", telegram_update, methods=["POST"]),
        Route("/jobs/weekly", weekly_job, methods=["POST"]),
        Route("/jobs/keepalive", keepalive_job, methods=["POST"]),
        Route("/", health, methods=["GET"]),
    ],
    lifespan=lifespan,
)
