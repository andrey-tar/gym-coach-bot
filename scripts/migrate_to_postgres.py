"""One-time copy of the local SQLite database into the cloud Postgres (Supabase).

    .venv\\Scripts\\python -m scripts.migrate_to_postgres "postgresql://..."            # dry run: counts only
    .venv\\Scripts\\python -m scripts.migrate_to_postgres "postgresql://..." --apply    # copy

Refuses to write into a target that already has rows, so running it twice can't duplicate data.
Source is data/coach.db (or --source path). Stop the local bot first so nothing changes mid-copy.
"""

import argparse
import sys
from pathlib import Path

from sqlalchemy import create_engine, func, select, text

from app.models import Base

ROOT = Path(__file__).resolve().parent.parent


def pg_url(url: str) -> str:
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url.removeprefix(prefix)
    return url


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="Postgres connection string (Supabase > Connect > Session pooler)")
    ap.add_argument("--source", default=str(ROOT / "data" / "coach.db"))
    ap.add_argument("--apply", action="store_true", help="actually copy (default: dry run)")
    args = ap.parse_args()

    src = create_engine(f"sqlite:///{Path(args.source).as_posix()}")
    dst = create_engine(pg_url(args.target))
    tables = Base.metadata.sorted_tables  # parents before children (foreign keys)

    Base.metadata.create_all(dst)
    with src.connect() as s, dst.connect() as d:
        busy = [t.name for t in tables if d.scalar(select(func.count()).select_from(t))]
        if busy:
            sys.exit(f"Target already has data in: {', '.join(busy)}. Nothing copied.")
        src_tables = set(s.execute(text("select name from sqlite_master where type='table'")).scalars())
        for t in tables:
            n = s.scalar(select(func.count()).select_from(t)) if t.name in src_tables else 0
            print(f"{t.name:22} {n:6} rows")
        if not args.apply:
            print("\nDry run - nothing copied. Add --apply to copy.")
            return

    # Raw rows (no ORM), so stored values move as they are; one transaction - all or nothing.
    with src.connect() as s, dst.begin() as d:
        for t in tables:
            if t.name not in src_tables:
                continue
            rows = [dict(r._mapping) for r in s.execute(select(t))]
            if rows:
                d.execute(t.insert(), rows)
            # Postgres id counters start at 1; move them past the copied ids.
            pk = list(t.primary_key.columns)
            if d.dialect.name == "postgresql" and len(pk) == 1 and pk[0].autoincrement is not False and pk[0].type.python_type is int and t.name != "processed_updates":
                d.execute(text(f"select setval(pg_get_serial_sequence('{t.name}', '{pk[0].name}'), "
                               f"coalesce((select max({pk[0].name}) from {t.name}), 0) + 1, false)"))
    with src.connect() as s, dst.connect() as d:
        bad = [t.name for t in tables if t.name in src_tables and
               s.scalar(select(func.count()).select_from(t)) != d.scalar(select(func.count()).select_from(t))]
    print("Copied, row counts match." if not bad else f"Row counts differ in: {', '.join(bad)}")


if __name__ == "__main__":
    main()
