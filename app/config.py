"""Environment-driven settings. Everything tunable at build time lives here (spec Section 13)."""

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    owner_id: int = _int("OWNER_TELEGRAM_ID", 0)

    llm_model: str = os.getenv("LLM_MODEL", "claude-sonnet-5")
    llm_effort: str = os.getenv("LLM_EFFORT", "medium")
    llm_synthesis_effort: str = os.getenv("LLM_SYNTHESIS_EFFORT", "high")
    handler_mode: str = os.getenv("HANDLER_MODE", "combined")

    database_url: str = os.getenv("DATABASE_URL", f"sqlite:///{(ROOT / 'data' / 'coach.db').as_posix()}")
    timezone: str = os.getenv("TIMEZONE", "Europe/Warsaw")

    webhook_secret: str = os.getenv("WEBHOOK_SECRET", "")
    jobs_secret: str = os.getenv("JOBS_SECRET", "")

    # Lazy-close staleness thresholds (hours of inactivity), spec 3.1 step 10 / Section 13.
    # A session with an accepted-but-unexecuted plan gets a long window so a morning plan
    # survives until an evening (or next-day) workout.
    stale_hours_logged: int = _int("STALE_HOURS_LOGGED", 4)
    stale_hours_planned: int = _int("STALE_HOURS_PLANNED", 36)
    stale_hours_talk: int = _int("STALE_HOURS_TALK", 12)

    # Out-of-session chat buffer: keeps standalone exchanges coherent ("show profile" -> "fix X").
    loose_buffer_minutes: int = _int("LOOSE_BUFFER_MINUTES", 120)
    loose_buffer_messages: int = _int("LOOSE_BUFFER_MESSAGES", 8)

    history_default_limit: int = _int("HISTORY_DEFAULT_LIMIT", 10)
    injury_decay_days: int = _int("INJURY_DECAY_DAYS", 30)
    weigh_in_reminder_days: int = _int("WEIGH_IN_REMINDER_DAYS", 7)
    # Rough rate for showing the weekly token spend in zł (the cost log itself is in USD).
    usd_pln: float = float(os.getenv("USD_PLN", "3.7") or 3.7)

    # Keep a copy of each closed session's conversation for review: always in the DB, plus JSON files in
    # TRANSCRIPT_DIR (set it empty on the cloud host, whose disk is wiped on every restart).
    keep_transcripts: bool = os.getenv("KEEP_TRANSCRIPTS", "1") == "1"
    transcript_dir: str = os.getenv("TRANSCRIPT_DIR", str(ROOT / "data" / "transcripts"))

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


settings = Settings()


# Test-only clock offset (scripted model comparison simulates days passing). Always 0 in the bot.
clock_offset = timedelta(0)


def now() -> datetime:
    """Timezone-aware current time in the owner's timezone (spec 2.1)."""
    return datetime.now(settings.tz) + clock_offset
