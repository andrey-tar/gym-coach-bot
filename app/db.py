"""Engine/session setup and first-run seeding (spec Section 6 "Initial state")."""

from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base, Profile, Split

MUSCLE_GROUPS = [
    "chest", "back", "shoulders", "biceps", "triceps", "quads", "hamstrings",
    "glutes", "calves", "core", "forearms", "traps", "lower_back",
]

MUSCLE_GROUPS_RU = {
    "chest": "грудь", "back": "спина", "shoulders": "плечи", "biceps": "бицепс",
    "triceps": "трицепс", "quads": "квадрицепс", "hamstrings": "бицепс бедра",
    "glutes": "ягодицы", "calves": "икры", "core": "пресс/кор", "forearms": "предплечья",
    "traps": "трапеции", "lower_back": "поясница",
}

DEFAULT_STRUCTURE = (
    "3 основных упражнения (обычный отдых между подходами) + 2 вспомогательных, "
    "выполняемых суперсетом без отдыха между ними. Общая длительность ≈ 2 часа: "
    "≈1 ч силовая + ≈1 ч кардио (по умолчанию дорожка)."
)

DEFAULT_SPLITS = [
    {
        "name": "Грудь",
        "claimed_groups": ["chest", "shoulders", "triceps"],
        "specialization": (
            "Отвечаешь за грудь, передние/средние дельты и трицепс. Приоритет — сохранить силу "
            "в жимах на дефиците калорий. Следи за плечевыми суставами. Если плечи или трицепс "
            "уже получили нагрузку в другой день — учитывай это, а не добивай фиксированный объём."
        ),
    },
    {
        "name": "Спина",
        "claimed_groups": ["back", "biceps", "traps", "shoulders", "lower_back", "forearms"],
        "specialization": (
            "Отвечаешь за спину (широчайшие, середина спины), трапеции, задние дельты и бицепс. "
            "Приоритет — тяги с контролем техники. Следи за хватом и предплечьями: если они "
            "отказывают раньше целевых мышц — это сигнал менять вес или упражнение."
        ),
    },
    {
        "name": "Ноги",
        "claimed_groups": ["quads", "hamstrings", "glutes", "calves", "core"],
        "specialization": (
            "Отвечаешь за ноги (квадрицепс, бицепс бедра, ягодицы, икры) и пресс. Особое внимание "
            "коленям: техника важнее веса. Прогрессию по весу вводи постепенно."
        ),
    },
]

_url = settings.database_url
if _url.startswith("sqlite:///"):
    Path(_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
elif _url.startswith("postgres://"):
    _url = "postgresql+psycopg://" + _url.removeprefix("postgres://")
elif _url.startswith("postgresql://"):
    _url = "postgresql+psycopg://" + _url.removeprefix("postgresql://")

engine = create_engine(_url, pool_pre_ping=True)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


@contextmanager
def db_session():
    session: Session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    Base.metadata.create_all(engine)
    if engine.dialect.name == "postgresql":
        # Supabase serves every public table over its Data API; RLS with no policies closes that door.
        # The bot connects as the table owner, which RLS doesn't restrict.
        with engine.begin() as conn:
            open_tables = conn.execute(text(
                "select relname from pg_class where relnamespace = 'public'::regnamespace "
                "and relkind = 'r' and not relrowsecurity")).scalars().all()  # one round trip on a warm DB
            for name in set(open_tables) & {t.name for t in Base.metadata.sorted_tables}:
                conn.execute(text(f'alter table "{name}" enable row level security'))
    with db_session() as db:
        if db.get(Profile, 1) is None:
            db.add(Profile(
                id=1,
                goals=["Сжигание жира с сохранением мышечной массы"],
                training_structure=DEFAULT_STRUCTURE,
                tone="Дружелюбный, прямой, по делу. Без воды и мотивационных лозунгов.",
                assertiveness=(
                    "Высокая: открыто спорить и аргументировать, если не согласен с решением; "
                    "последнее слово всё равно за пользователем."
                ),
                footnote_style="Коротко, одной строкой.",
                default_cardio_modality="treadmill",
            ))
        if db.scalar(select(Split).limit(1)) is None:
            for split in DEFAULT_SPLITS:
                db.add(Split(**split))
