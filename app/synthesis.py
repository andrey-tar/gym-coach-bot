"""End-of-session synthesis (spec Section 5).

The per-entity steps (`synthesize_exercise_note`, `synthesize_session`) are the same functions the
one-time history import replays row by row (spec Appendix).
"""

import asyncio
import json
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import llm
from .config import now, settings
from .context import fmt_date, fmt_num, fmt_perf, fmt_triple, groups_ru, prev_performance, time_block
from .db import MUSCLE_GROUPS
from .models import ArchivedTranscript, BodyMetric, CardioRecord, Exercise, Injury, Performance, Profile, Split, TrainingSession
from .prompts import EXERCISE_NOTE_SYSTEM, SESSION_SYNTHESIS_SYSTEM

log = logging.getLogger(__name__)

NULLABLE_STR = {"anyOf": [{"type": "string"}, {"type": "null"}]}
NULLABLE_NUM = {"anyOf": [{"type": "number"}, {"type": "null"}]}

SESSION_SCHEMA = {
    "type": "object",
    "properties": {
        "chat_summary": {"type": "string"},
        "split_note": NULLABLE_STR,
        "general_state": NULLABLE_STR,
        "cardio_note": NULLABLE_STR,
        "treadmill_note": NULLABLE_STR,
        "goals": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
        "injuries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "reinforce", "resolve"]},
                    "injury_id": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "description": {"type": "string"},
                    "body_area": {"type": "string"},
                    "muscle_groups": {"type": "array", "items": {"type": "string", "enum": MUSCLE_GROUPS}},
                },
                "required": ["action", "injury_id", "description", "body_area", "muscle_groups"],
                "additionalProperties": False,
            },
        },
        "body_metrics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"weight_kg": NULLABLE_NUM, "muscle_kg": NULLABLE_NUM, "fat_pct": NULLABLE_NUM},
                "required": ["weight_kg", "muscle_kg", "fat_pct"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["chat_summary", "split_note", "general_state", "cardio_note", "treadmill_note", "goals", "injuries", "body_metrics"],
    "additionalProperties": False,
}


def _transcript_text(transcript: list[dict]) -> str:
    who = {"user": "Клиент", "assistant": "Тренер"}
    return "\n".join(f"{who.get(m['role'], m['role'])}: {m['content']}" for m in transcript) or "(no conversation)"


async def synthesize_exercise_note(*, exercise_name: str, muscle_groups: list[str], current_note: str,
                                   session_rows: list[str], session_context: str, date_text: str) -> str:
    user = (
        f"Exercise: {exercise_name} [{', '.join(muscle_groups)}]\n"
        f"Current note: {current_note or '(none yet)'}\n\n"
        f"New session ({date_text}) - logged result(s) for this exercise:\n" + "\n".join(session_rows) +
        f"\n\nSession context (conversation / other remarks that may explain the result):\n{session_context}"
    )
    result = await llm.structured(
        stable_system=EXERCISE_NOTE_SYSTEM, user=user, max_tokens=8000, purpose="note",
        schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"],
                "additionalProperties": False},
    )
    return result["note"].strip()


async def synthesize_session(*, profile_text: str, split_text: str, session_text: str, already_applied: list[str]) -> dict:
    user = (
        f"{profile_text}\n\n{split_text}\n\n## Session\n{session_text}\n\n"
        f"## Already applied live during the session (do not repeat)\n" + ("\n".join(already_applied) or "(nothing)")
    )
    return await llm.structured(stable_system=SESSION_SYNTHESIS_SYSTEM, user=user, purpose="session_synthesis",
                                schema=SESSION_SCHEMA, max_tokens=8000)


def profile_snapshot(db: Session) -> str:
    p = db.get(Profile, 1)
    injuries = db.scalars(select(Injury).where(Injury.status == "active")).all()
    inj = "; ".join(f"#{i.id} {i.description} [{', '.join(i.muscle_groups)}]" for i in injuries) or "none"
    return (f"{time_block()}\n## Current stored profile\nGoals: {'; '.join(p.goals)}\n"
            f"General state: {p.general_state or '—'}\nCardio fitness note: {p.cardio_note or '—'}\n"
            f"Treadmill trend note: {p.treadmill_note or '—'}\nActive injuries: {inj}")


def cardio_text(c: CardioRecord) -> str:
    zones = ", ".join(f"Z{i + 1} {s // 60}:{s % 60:02d}" for i, s in enumerate(c.zone_seconds)) or "—"
    extra = (", ".join(f"{i.get('label', '')} {fmt_num(i.get('incline_pct'))}%/{fmt_num(i.get('speed_kmh'))} км/ч"
                       for i in c.treadmill_intervals) if c.modality == "treadmill"
             else f"elliptical level {fmt_num(c.elliptical_level)}")
    return (f"Cardio: {c.modality}, {fmt_num(c.duration_min)} min, zones {zones}, {fmt_num(c.calories)} kcal, "
            f"carbs/protein/fat {fmt_num(c.carbs_pct)}/{fmt_num(c.protein_pct)}/{fmt_num(c.fat_pct)}%, {extra}. "
            f"Client comment: {c.comment or '—'}")


def apply_session_result(db: Session, result: dict, split: Split | None, when) -> list[str]:
    """Writes the session-level synthesis; returns footnote fragments for state changes."""
    p = db.get(Profile, 1)
    notes = []
    if split and result.get("split_note"):
        split.rolling_note = result["split_note"]
    if result.get("general_state"):
        p.general_state = result["general_state"]
    if result.get("cardio_note"):
        p.cardio_note = result["cardio_note"]
    if result.get("treadmill_note"):
        p.treadmill_note = result["treadmill_note"]
    if result.get("goals"):
        p.goals = result["goals"]
        notes.append("цели: " + "; ".join(p.goals))
    for inj in result.get("injuries") or []:
        if inj["action"] == "create":
            row = Injury(description=inj["description"], body_area=inj["body_area"], muscle_groups=inj["muscle_groups"],
                         status="active", created_at=when, last_mentioned_at=when, temporary_substitutions=[])
            db.add(row)
            notes.append(f"травма: {row.description} ({groups_ru(row.muscle_groups)})")
            continue
        row = db.get(Injury, inj["injury_id"]) if inj.get("injury_id") else None
        if row is None:
            continue
        row.last_mentioned_at = when
        if inj["action"] == "resolve" and row.status == "active":
            row.status, row.resolved_at = "resolved", when
            notes.append(f"травма «{row.description}» закрыта {fmt_date(when)}")
    for m in result.get("body_metrics") or []:
        if any(m.get(k) is not None for k in ("weight_kg", "muscle_kg", "fat_pct")):
            db.add(BodyMetric(measured_at=when, weight_kg=m.get("weight_kg"), muscle_kg=m.get("muscle_kg"),
                              fat_pct=m.get("fat_pct")))
            notes.append(f"замер: {fmt_num(m.get('weight_kg'))} кг")
    return notes


def archive_transcript(db: Session, session: TrainingSession, summary: str) -> None:
    """Copy of the conversation for reviewing the bot's replies later (not read by the bot):
    a DB row (survives on the cloud host) plus a local JSON file when TRANSCRIPT_DIR is set."""
    if not settings.keep_transcripts or not session.transcript:
        return
    db.merge(ArchivedTranscript(session_id=session.id, archived_at=now(), model=llm.model_name(),
                                transcript=session.transcript, summary=summary))
    if not settings.transcript_dir:
        return
    folder = Path(settings.transcript_dir)
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"session_id": session.id, "model": llm.model_name(), "transcript": session.transcript,
               "summary": summary}
    (folder / f"{fmt_date(session.started_at)}_session{session.id}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


async def close_session(db: Session, session: TrainingSession) -> tuple[str, str]:
    """Runs the full pipeline and closes the session.
    Returns (consolidated footnote, chat summary for the client); either may be ''."""
    performances = db.scalars(select(Performance).where(Performance.session_id == session.id)
                              .order_by(Performance.id)).all()
    cardio = db.scalar(select(CardioRecord).where(CardioRecord.session_id == session.id))

    # Plan lifecycle: accepted -> fulfilled (anything logged) | abandoned (spec 6.4).
    plan_note = ""
    for plan in session.plans:
        if plan.status != "accepted":
            continue
        item_ids = {i.id for i in plan.items}
        if any(p.plan_item_id in item_ids for p in performances):
            plan.status = "fulfilled"
        else:
            plan.status = "abandoned"
            plan_note = "план не выполнен (отмечен как abandoned)"

    notes: list[str] = []
    summary = ""
    if performances or cardio:
        when = session.started_at
        split = session.primary_split
        convo = _transcript_text(session.transcript)

        by_exercise: dict[int, list[Performance]] = {}
        for perf in performances:
            by_exercise.setdefault(perf.exercise_id, []).append(perf)

        async def one(ex_id: int, rows: list[Performance]) -> tuple[int, str]:
            ex = db.get(Exercise, ex_id)
            note = await synthesize_exercise_note(
                exercise_name=ex.name, muscle_groups=ex.muscle_groups, current_note=ex.rolling_note,
                session_rows=[fmt_perf(r) for r in rows], session_context=convo, date_text=fmt_date(when))
            return ex_id, note

        session_lines = [f"Date: {fmt_date(when)}; primary split: {split.name if split else '—'}"]
        targets = {i.id: f"{fmt_num(i.target_weight)}×{fmt_num(i.target_reps)}×{fmt_num(i.target_sets)}"
                   for plan in session.plans for i in plan.items}
        session_lines += [f"- {perf.exercise.name}: {fmt_perf(perf)} (target {targets.get(perf.plan_item_id, '—')}; "
                          f"previous session {fmt_triple(prev_performance(db, perf.exercise_id, session.id))})"
                          for perf in performances]
        if cardio:
            session_lines.append(cardio_text(cardio))
        session_lines.append(f"\nConversation:\n{convo}")
        split_text = (f"## Primary split: {split.name}\nCurrent split note: {split.rolling_note or '—'}"
                      if split else "## No primary split")

        results = await asyncio.gather(
            *(one(ex_id, rows) for ex_id, rows in by_exercise.items()),
            synthesize_session(profile_text=profile_snapshot(db), split_text=split_text,
                               session_text="\n".join(session_lines), already_applied=session.applied_changes),
        )
        for ex_id, note in results[:-1]:
            db.get(Exercise, ex_id).rolling_note = note
        summary = (results[-1].get("chat_summary") or "").strip()
        notes += apply_session_result(db, results[-1], split, now())
        updated = [db.get(Exercise, ex_id).name for ex_id in by_exercise]
        if updated:
            notes.insert(0, f"обновил заметки по {len(updated)} упражнениям" + (" и сплиту" if split else ""))
        if cardio:
            notes.append("кардио-заметка обновлена")

    if plan_note:
        notes.append(plan_note)
    archive_transcript(db, session, summary)
    session.status = "closed"
    session.closed_at = now()
    session.transcript = []          # working memory is discarded (spec Section 2)
    session.pending_entry = None
    return "; ".join(notes), summary


def stale_threshold_hours(db: Session, session: TrainingSession) -> int:
    logged = db.scalar(select(Performance.id).where(Performance.session_id == session.id).limit(1))
    if logged or db.scalar(select(CardioRecord.id).where(CardioRecord.session_id == session.id).limit(1)):
        return settings.stale_hours_logged
    if any(p.status == "accepted" for p in session.plans):
        return settings.stale_hours_planned
    return settings.stale_hours_talk


def is_stale(db: Session, session: TrainingSession) -> bool:
    age = now() - session.last_activity_at
    return age.total_seconds() > stale_threshold_hours(db, session) * 3600
