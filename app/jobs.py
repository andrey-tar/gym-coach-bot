"""Weekly scheduled job: weigh-in reminder + backup export (spec Sections 8, 9)."""

import json
from datetime import timedelta

from sqlalchemy import func, inspect, select

from . import telegram
from .config import now, settings
from .db import db_session
from .models import Base, BodyMetric, LlmUsage


def export_all() -> bytes:
    """Every persistent table (profile, splits, exercises with notes, full numeric history)."""
    data = {"exported_at": now().isoformat()}
    with db_session() as db:
        for mapper in Base.registry.mappers:
            cls = mapper.class_
            if cls.__tablename__ in ("processed_updates", "llm_usage", "archived_transcripts"):  # bookkeeping, not coach memory
                continue
            rows = []
            for obj in db.scalars(select(cls)).all():
                row = {}
                for col in inspect(cls).columns:
                    value = getattr(obj, col.key)
                    row[col.key] = value.isoformat() if hasattr(value, "isoformat") else value
                rows.append(row)
            data[cls.__tablename__] = rows
    return json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8")


def month_cost_text() -> str:
    """Model spend so far this calendar month (owner's timezone), from the per-call usage rows."""
    start = now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with db_session() as db:
        rows = db.execute(select(LlmUsage.model, func.sum(LlmUsage.cost_usd), func.count())
                          .where(LlmUsage.ts >= start).group_by(LlmUsage.model)).all()
    total = sum(cost or 0 for _, cost, _ in rows)
    per_model = ", ".join(f"{m}: ${cost or 0:.2f}, запросов: {n}" for m, cost, n in rows) or "запросов не было"
    return (f"💸 Токены за {start.strftime('%m.%Y')}: ≈ ${total:.2f} (≈ {total * settings.usd_pln:.0f} zł) — {per_model}.\n"
            "Это оценка по прайсу без НДС; точная сумма и баланс — в AI Studio → Billing.")


async def run_weekly() -> str:
    chat_id = settings.owner_id
    t = now()
    with db_session() as db:
        last = db.scalar(select(BodyMetric).order_by(BodyMetric.measured_at.desc()))
    if last is None or t - last.measured_at > timedelta(days=settings.weigh_in_reminder_days):
        await telegram.send_message(chat_id, "⚖️ Неделю не было замеров. Взвесься и пришли вес / мышцы / % жира из приложения весов.")
    await telegram.send_document(chat_id, f"coach_backup_{t.strftime('%Y-%m-%d')}.json", export_all(),
                                 caption="Еженедельная резервная копия данных тренера.")
    await telegram.send_message(chat_id, month_cost_text())
    return "ok"


if __name__ == "__main__":
    import asyncio
    print(asyncio.run(run_weekly()))
