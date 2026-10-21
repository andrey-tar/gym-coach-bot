"""Bounded, purpose-built context assembly for every model call (spec Sections 2, 3.1, 4)."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import now, settings
from .db import MUSCLE_GROUPS_RU
from .models import BodyMetric, CardioRecord, Exercise, Injury, Performance, Profile, Split, TrainingSession

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def fmt_dt(dt: datetime) -> str:
    return dt.astimezone(settings.tz).strftime("%Y-%m-%d %H:%M")


def fmt_date(dt: datetime) -> str:
    return dt.astimezone(settings.tz).strftime("%Y-%m-%d")


def fmt_num(x: float | None) -> str:
    if x is None:
        return "?"
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def groups_ru(groups: list[str]) -> str:
    return ", ".join(MUSCLE_GROUPS_RU.get(g, g) for g in groups) or "—"


def fmt_perf(p: Performance) -> str:
    text = f"{fmt_num(p.weight_kg)} кг × {fmt_num(p.reps)} × {fmt_num(p.sets)}"
    return text + (f" — «{p.comment}»" if p.comment else "")


def overlaps(a: list[str], b: list[str]) -> bool:
    return bool(set(a) & set(b))


def open_session(db: Session) -> TrainingSession | None:
    return db.scalar(select(TrainingSession).where(TrainingSession.status == "open")
                     .order_by(TrainingSession.id.desc()))


def last_performance(db: Session, exercise_id: int) -> Performance | None:
    return db.scalar(select(Performance).where(Performance.exercise_id == exercise_id)
                     .order_by(Performance.performed_at.desc(), Performance.id.desc()))


def prev_performance(db: Session, exercise_id: int, session_id: int) -> Performance | None:
    """Latest result of an exercise from an earlier session (today's own logs excluded)."""
    return db.scalar(select(Performance).where(Performance.exercise_id == exercise_id,
                                               Performance.session_id != session_id)
                     .order_by(Performance.performed_at.desc(), Performance.id.desc()))


def fmt_triple(p: Performance | None) -> str:
    return f"{fmt_num(p.weight_kg)}×{fmt_num(p.reps)}×{fmt_num(p.sets)}" if p else "—"


def time_block() -> str:
    t = now()
    return f"Current date/time: {t.strftime('%Y-%m-%d %H:%M')} ({WEEKDAYS_RU[t.weekday()]}), timezone {settings.timezone}."


def profile_block(db: Session) -> str:
    p = db.get(Profile, 1)
    t = now()
    lines = [time_block(), "", "## Client profile (global layer)"]
    lines.append("Goals: " + ("; ".join(p.goals) if p.goals else "—"))
    lines.append(f"Training-structure preference (global): {p.training_structure}")
    lines.append(f"Tone: {p.tone}")
    lines.append(f"Coaching assertiveness: {p.assertiveness}")
    lines.append(f"General state note: {p.general_state or '—'}")

    metrics = db.scalars(select(BodyMetric).order_by(BodyMetric.measured_at.desc()).limit(5)).all()
    if metrics:
        days = (t - metrics[0].measured_at).days
        lines.append(f"Body metrics (newest first; last weigh-in {days} days ago):")
        for m in metrics:
            lines.append(f"  {fmt_date(m.measured_at)}: вес {fmt_num(m.weight_kg)} кг, "
                         f"мышцы {fmt_num(m.muscle_kg)} кг, жир {fmt_num(m.fat_pct)}%")
    else:
        lines.append("Body metrics: none logged yet.")

    injuries = db.scalars(select(Injury).where(Injury.status == "active")).all()
    if injuries:
        lines.append("Active injuries:")
        for inj in injuries:
            silent = (t - inj.last_mentioned_at).days
            flag = (f" [NOT MENTIONED FOR {silent} DAYS - if relevant today, ask briefly if it's still an issue]"
                    if silent >= settings.injury_decay_days else "")
            subs = f"; temporary substitutions: {', '.join(inj.temporary_substitutions)}" if inj.temporary_substitutions else ""
            lines.append(f"  #{inj.id} {inj.description} (area: {inj.body_area or '—'}; groups: "
                         f"{', '.join(inj.muscle_groups)}; since {fmt_date(inj.created_at)}; last mentioned "
                         f"{silent} days ago){subs}{flag}")
    else:
        lines.append("Active injuries: none.")
    to_revert = db.scalars(select(Injury).where(Injury.status == "resolved", Injury.reverts_proposed.is_(False))).all()
    for inj in to_revert:
        if inj.temporary_substitutions:
            lines.append(f"Resolved injury #{inj.id} ({inj.description}) had temporary substitutions "
                         f"{inj.temporary_substitutions} - propose reverting them when relevant.")

    baseline = ", ".join(f"{i.get('label') or 'интервал'} {fmt_num(i.get('incline_pct'))}%/{fmt_num(i.get('speed_kmh'))} км/ч"
                         for i in p.treadmill_baseline) or "не задан"
    lines.append(f"Cardio: default modality {p.default_cardio_modality}; treadmill baseline: {baseline}; "
                 f"treadmill trend note: {p.treadmill_note or '—'}; cardio fitness note: {p.cardio_note or '—'}")
    return "\n".join(lines)


def splits_block(db: Session) -> str:
    """Split list + lightweight recent-load-per-split snapshot (spec 4)."""
    t = now()
    splits = db.scalars(select(Split).order_by(Split.id)).all()
    week_perfs = db.scalars(select(Performance).where(Performance.performed_at >= t - timedelta(days=7))).all()
    lines = ["## Splits (recent load snapshot)"]
    for s in splits:
        last = db.scalar(select(TrainingSession).where(TrainingSession.primary_split_id == s.id,
                                                       TrainingSession.status == "closed")
                         .order_by(TrainingSession.started_at.desc()))
        touching = [p for p in week_perfs if overlaps(p.exercise.muscle_groups, s.claimed_groups)]
        sets = sum(p.sets or 0 for p in touching)
        if last and last.imported:
            last_txt = (f"last trained ~{fmt_date(last.started_at)} (PLACEHOLDER date from imported history - real date "
                        f"unknown; do not draw recovery/frequency conclusions from it)")
        elif last:
            last_txt = f"last trained {fmt_date(last.started_at)} ({(t - last.started_at).days} d ago)"
        else:
            last_txt = "never trained"
        status = "" if s.status == "active" else " [ARCHIVED]"
        lines.append(f"- {s.name}{status}: groups [{', '.join(s.claimed_groups)}]; {last_txt}; "
                     f"last 7 days: {len(touching)} exercises / {sets} sets touching its groups")
    return "\n".join(lines)


def registry_block(db: Session) -> str:
    """Names only - for identity resolution (spec 4.2); notes are pulled per split."""
    exercises = db.scalars(select(Exercise).order_by(Exercise.name)).all()
    if not exercises:
        return "## Exercise registry\n(empty)"
    rows = [f"- {e.name} [{', '.join(e.muscle_groups)}]{' (archived)' if e.status == 'archived' else ''}"
            for e in exercises]
    return "## Exercise registry (all known names)\n" + "\n".join(rows)


def split_context(db: Session, split: Split, include_archived: bool = False) -> str:
    """The active split's own context: config, rolling note, active exercises + notes (spec 3.1 step 2, 4.3)."""
    p = db.get(Profile, 1)
    lines = [f"## Today's split: {split.name}",
             f"Claimed muscle groups: {', '.join(split.claimed_groups)}",
             f"Specialization instructions: {split.specialization or '—'}",
             f"Training structure: {split.structure_override or p.training_structure}"
             + (" (split override)" if split.structure_override else " (global default)"),
             f"Split rolling note: {split.rolling_note or '—'}",
             "Exercises (overlap with claimed groups):"]
    exercises = db.scalars(select(Exercise).order_by(Exercise.name)).all()
    shown = 0
    for e in exercises:
        if not overlaps(e.muscle_groups, split.claimed_groups):
            continue
        if e.status != "active" and not include_archived:
            continue
        last = last_performance(db, e.id)
        last_txt = f"last: {fmt_date(last.performed_at)} {fmt_num(last.weight_kg)}×{fmt_num(last.reps)}×{fmt_num(last.sets)}" if last else "never performed"
        status = " (archived)" if e.status == "archived" else ""
        lines.append(f"- {e.name}{status} [{', '.join(e.muscle_groups)}]; {last_txt}\n  note: {e.rolling_note or '—'}")
        shown += 1
    if not shown:
        lines.append("(no exercises yet - propose new ones from your own knowledge)")
    return "\n".join(lines)


def session_block(db: Session, session: TrainingSession) -> str:
    lines = [f"## Open session (started {fmt_dt(session.started_at)})"]
    plan = session.plans[-1] if session.plans else None
    if plan:
        lines.append(f"Plan status: {plan.status} (finalized {fmt_dt(plan.finalized_at)})")
        for item in plan.items:
            perf = db.scalar(select(Performance).where(Performance.plan_item_id == item.id))
            target = f"{fmt_num(item.target_weight)}×{fmt_num(item.target_reps)}×{fmt_num(item.target_sets)}"
            sub = " (on-the-spot substitution)" if item.substitutes_item_id else ""
            ss = f" [superset {item.superset_group}]" if item.superset_group else ""
            result = f" -> LOGGED {fmt_perf(perf)}" if perf else f" -> {item.status}"
            prev = fmt_triple(prev_performance(db, item.exercise_id, session.id))
            lines.append(f"- {item.exercise.name}{sub}{ss}: previous {prev}; target {target}{result}")
        if plan.cardio_plan:
            lines.append(f"Planned cardio: {plan.cardio_plan}")
    else:
        lines.append("No plan finalized yet (negotiation phase).")
    cardio = db.scalar(select(CardioRecord).where(CardioRecord.session_id == session.id))
    if cardio:
        lines.append(f"Cardio logged: {cardio.modality}, {fmt_num(cardio.duration_min)} min")
    lines.append(f"Strength block marked done: {'yes' if session.strength_done else 'no'}")
    return "\n".join(lines)


def build_dynamic_context(db: Session, session: TrainingSession | None) -> str:
    parts = [profile_block(db), splits_block(db), registry_block(db)]
    if session and session.primary_split:
        parts.append(split_context(db, session.primary_split))
        parts.append(session_block(db, session))
    elif session:
        parts.append(session_block(db, session))
    else:
        parts.append("## No open training session. Standalone messages (weight, goals, questions, profile edits)"
                     " are handled without starting one.")
    return "\n\n".join(parts)
