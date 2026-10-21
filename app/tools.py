"""Tools the model uses to read history and write state.

Every write returns a short Russian confirmation that the app collects into the footnote
(spec 5: the footnote mechanism is code, not a setting).
"""

import re
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import llm
from .config import now, settings
from .context import fmt_date, fmt_num, fmt_perf, groups_ru, open_session, overlaps, split_context
from .db import MUSCLE_GROUPS, MUSCLE_GROUPS_RU
from .models import (BodyMetric, CardioRecord, Exercise, Injury, Performance, Plan, PlanItem, Profile, Split,
                     TrainingSession)
from .prompts import CLASSIFY_SYSTEM

GROUPS_SCHEMA = {"type": "array", "items": {"type": "string", "enum": MUSCLE_GROUPS}}
INTERVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "description": "e.g. 'гора', 'предгорье'"},
        "incline_pct": {"type": "number"},
        "speed_kmh": {"type": "number"},
    },
}
PLAN_EXERCISE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Exact registry name, or the full specific name if new."},
        "muscle_groups": {**GROUPS_SCHEMA, "description": "Required only for exercises not in the registry."},
        "weight_kg": {"type": "number"},
        "reps": {"type": "integer"},
        "sets": {"type": "integer"},
        "superset_group": {"type": "string", "description": "Same letter for exercises done as a superset, e.g. 'A'."},
        "note": {"type": "string"},
    },
    "required": ["name"],
}

READ_TOOLS = [
    {
        "name": "query_history",
        "description": "Raw performance history for on-demand analysis (progress, 'is today fair', trends). "
                       "Ignores active/archived status. Filter by exercise names, muscle groups, or split; "
                       "optionally include cardio records. Default: last 10 performances per exercise.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exercise_names": {"type": "array", "items": {"type": "string"}},
                "muscle_groups": GROUPS_SCHEMA,
                "split_name": {"type": "string"},
                "since_date": {"type": "string", "description": "YYYY-MM-DD"},
                "limit_per_exercise": {"type": "integer"},
                "include_cardio": {"type": "boolean"},
                "include_body_metrics": {"type": "boolean"},
            },
        },
    },
    {
        "name": "list_exercises",
        "description": "Exercises overlapping a split's (or given) muscle groups, with rolling notes and last "
                       "result. include_archived=true for variety requests, 'what did I use to do', or new/reconfigured splits.",
        "input_schema": {
            "type": "object",
            "properties": {
                "split_name": {"type": "string"},
                "muscle_groups": GROUPS_SCHEMA,
                "include_archived": {"type": "boolean"},
            },
        },
    },
    {
        "name": "show_profile",
        "description": "Full readable dump of stored state for the show/edit request: goals, injuries (incl. resolved), "
                       "body metrics, notes, splits with specializations, exercise registry with statuses and notes, "
                       "cardio settings, tone/assertiveness/footnote style.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sections": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["profile", "injuries", "metrics", "splits", "exercises", "cardio", "behavior"]},
                    "description": "Omit for everything.",
                },
            },
        },
    },
]

WRITE_TOOLS = [
    {
        "name": "start_session",
        "description": "Open (or re-target) today's training session under a split. Returns that split's context.",
        "input_schema": {"type": "object", "properties": {"split_name": {"type": "string"}}, "required": ["split_name"]},
    },
    {
        "name": "finalize_plan",
        "description": "Lock the agreed plan (client clearly agreed). Creates logging buttons. New exercises are "
                       "created as active.",
        "input_schema": {
            "type": "object",
            "properties": {
                "exercises": {"type": "array", "items": PLAN_EXERCISE_SCHEMA},
                "cardio": {
                    "type": "object",
                    "properties": {
                        "modality": {"type": "string", "enum": ["treadmill", "elliptical"]},
                        "duration_min": {"type": "number"},
                        "intervals": {"type": "array", "items": INTERVAL_SCHEMA},
                        "elliptical_level": {"type": "number"},
                    },
                },
            },
            "required": ["exercises"],
        },
    },
    {
        "name": "add_substitution",
        "description": "Client actually did a different exercise instead of a planned one (on-the-spot). The planned "
                       "one becomes not-done; the substitute gets its own logging button. A brand-new substitute is "
                       "created archived (not in rotation). Pass the numbers if the client already gave them.",
        "input_schema": {
            "type": "object",
            "properties": {
                "replaces": {"type": "string", "description": "Planned exercise name (omit if it's an extra, not a swap)."},
                "name": {"type": "string"},
                "muscle_groups": GROUPS_SCHEMA,
                "weight_kg": {"type": "number"},
                "reps": {"type": "integer"},
                "sets": {"type": "integer"},
                "comment": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "log_cardio",
        "description": "Log today's cardio from the client's heart-rate-monitor readout. Times as seconds.",
        "input_schema": {
            "type": "object",
            "properties": {
                "modality": {"type": "string", "enum": ["treadmill", "elliptical"]},
                "duration_min": {"type": "number"},
                "zone_seconds": {"type": "array", "items": {"type": "integer"}, "description": "[z1,z2,z3,z4,z5] in seconds"},
                "calories": {"type": "integer"},
                "carbs_pct": {"type": "number"},
                "protein_pct": {"type": "number"},
                "fat_pct": {"type": "number"},
                "intervals": {"type": "array", "items": INTERVAL_SCHEMA},
                "elliptical_level": {"type": "number"},
                "comment": {"type": "string", "description": "Client's qualitative remark about heart-rate response, verbatim-ish."},
            },
            "required": ["modality"],
        },
    },
    {
        "name": "close_session",
        "description": "The client said the workout is over. Triggers end-of-session synthesis.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "log_body_metric",
        "description": "Log a body measurement (from the scale app). Any subset of fields.",
        "input_schema": {
            "type": "object",
            "properties": {
                "weight_kg": {"type": "number"},
                "muscle_kg": {"type": "number"},
                "fat_pct": {"type": "number"},
                "date": {"type": "string", "description": "YYYY-MM-DD if not today"},
            },
        },
    },
    {
        "name": "update_goals",
        "description": "Replace the goal list (explicit statement only).",
        "input_schema": {"type": "object", "properties": {"goals": {"type": "array", "items": {"type": "string"}}},
                         "required": ["goals"]},
    },
    {
        "name": "update_injury",
        "description": "create: new injury; reinforce: client mentioned it again (still there); resolve: client says "
                       "it's fine now; update: correct description/groups; add a temporary_substitution made because of it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "reinforce", "resolve", "update"]},
                "injury_id": {"type": "integer"},
                "description": {"type": "string"},
                "body_area": {"type": "string"},
                "muscle_groups": GROUPS_SCHEMA,
                "temporary_substitution": {"type": "string", "description": "e.g. 'Жим ногами -> Разгибания ног' or 'дорожка -> орбитрек'"},
                "reverts_proposed": {"type": "boolean", "description": "true once you've proposed reverting its substitutions"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "update_split",
        "description": "Create / rename / reconfigure / archive / restore a split. Create requires claimed_groups and an "
                       "approved specialization text.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "update", "archive", "restore"]},
                "split_name": {"type": "string"},
                "new_name": {"type": "string"},
                "claimed_groups": GROUPS_SCHEMA,
                "specialization": {"type": "string"},
                "structure_override": {"type": "string", "description": "Empty string removes the override."},
            },
            "required": ["action", "split_name"],
        },
    },
    {
        "name": "upsert_exercise",
        "description": "Create an exercise or change one: rename, fix muscle groups, set status active/archived "
                       "(explicit permanent rotation changes only).",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "new_name": {"type": "string"},
                "muscle_groups": GROUPS_SCHEMA,
                "status": {"type": "string", "enum": ["active", "archived"]},
            },
            "required": ["name"],
        },
    },
    {
        "name": "update_settings",
        "description": "Change standing preferences: training structure, tone, assertiveness, footnote style, default "
                       "cardio modality, treadmill baseline profile.",
        "input_schema": {
            "type": "object",
            "properties": {
                "training_structure": {"type": "string"},
                "tone": {"type": "string"},
                "assertiveness": {"type": "string"},
                "footnote_style": {"type": "string"},
                "default_cardio_modality": {"type": "string", "enum": ["treadmill", "elliptical"]},
                "treadmill_baseline": {"type": "array", "items": INTERVAL_SCHEMA},
            },
        },
    },
    {
        "name": "edit_note",
        "description": "Correct a stored rolling note when the client says it's wrong.",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "enum": ["general_state", "cardio_note", "treadmill_note", "split_note", "exercise_note"]},
                "name": {"type": "string", "description": "Split or exercise name for split_note / exercise_note."},
                "text": {"type": "string"},
            },
            "required": ["target", "text"],
        },
    },
]

READ_NAMES = {t["name"] for t in READ_TOOLS}


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower().replace("ё", "е"))


def find_exercise(db: Session, name: str) -> Exercise | None:
    target = _norm(name)
    for e in db.scalars(select(Exercise)).all():
        if _norm(e.name) == target:
            return e
    return None


def find_split(db: Session, name: str) -> Split | None:
    target = _norm(name)
    for s in db.scalars(select(Split)).all():
        if _norm(s.name) == target:
            return s
    return None


async def classify_muscle_groups(name: str) -> list[str]:
    result = await llm.structured(
        stable_system=CLASSIFY_SYSTEM, effort="low", max_tokens=2000, purpose="classify",
        user=f"Exercise: {name}",
        schema={"type": "object", "properties": {"muscle_groups": GROUPS_SCHEMA},
                "required": ["muscle_groups"], "additionalProperties": False},
    )
    return [g for g in result["muscle_groups"] if g in MUSCLE_GROUPS][:4]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        d = datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return d.replace(hour=12, tzinfo=settings.tz)


class ToolContext:
    """Executes tools for one user turn and records side effects for the handler."""

    def __init__(self, db: Session, transcript_seed: list[dict] | None = None):
        self.db = db
        self.footnotes: list[str] = []
        self.show_keyboard = False
        self.close_requested = False
        self.transcript_seed = transcript_seed or []

    @property
    def session(self) -> TrainingSession | None:
        return open_session(self.db)

    def _note(self, text: str) -> str:
        self.footnotes.append(text)
        s = self.session
        if s is not None:
            s.applied_changes = [*s.applied_changes, text]
        return f"OK: {text}"

    async def get_or_create_exercise(self, name: str, groups: list[str] | None, status: str) -> tuple[Exercise, bool]:
        existing = find_exercise(self.db, name)
        if existing:
            return existing, False
        groups = [g for g in (groups or []) if g in MUSCLE_GROUPS] or await classify_muscle_groups(name)
        ex = Exercise(name=name.strip(), muscle_groups=groups, status=status, created_at=now())
        self.db.add(ex)
        self.db.flush()
        return ex, True

    async def execute(self, name: str, args: dict) -> str:
        handler = getattr(self, f"t_{name}", None)
        if handler is None:
            return f"Error: unknown tool {name}"
        result = await handler(**args)
        self.db.flush()
        return result

    # ---- read tools ----

    async def t_query_history(self, exercise_names=None, muscle_groups=None, split_name=None, since_date=None,
                              limit_per_exercise=None, include_cardio=False, include_body_metrics=False) -> str:
        db = self.db
        since = _parse_date(since_date)
        limit = limit_per_exercise or (None if since else settings.history_default_limit)
        groups = list(muscle_groups or [])
        if split_name:
            split = find_split(db, split_name)
            if split:
                groups += split.claimed_groups
        exercises = []
        for n in exercise_names or []:
            e = find_exercise(db, n)
            if e:
                exercises.append(e)
        if groups:
            exercises += [e for e in db.scalars(select(Exercise)).all() if overlaps(e.muscle_groups, groups) and e not in exercises]
        out = []
        for e in exercises:
            q = select(Performance).where(Performance.exercise_id == e.id)
            if since:
                q = q.where(Performance.performed_at >= since)
            rows = db.scalars(q.order_by(Performance.performed_at.desc()).limit(limit or 1000)).all()
            if rows:
                out.append(f"{e.name}:")
                out += [f"  {fmt_date(p.performed_at)}: {fmt_perf(p)}" for p in reversed(rows)]
        if include_cardio:
            q = select(CardioRecord)
            if since:
                q = q.where(CardioRecord.performed_at >= since)
            rows = db.scalars(q.order_by(CardioRecord.performed_at.desc()).limit(limit or 1000)).all()
            if rows:
                out.append("Cardio:")
                for c in reversed(rows):
                    zones = "/".join(f"{s // 60}м" for s in c.zone_seconds) if c.zone_seconds else "?"
                    extra = (", ".join(f"{i.get('label', '')} {fmt_num(i.get('incline_pct'))}%/{fmt_num(i.get('speed_kmh'))}"
                                       for i in c.treadmill_intervals) if c.modality == "treadmill"
                             else f"уровень {fmt_num(c.elliptical_level)}")
                    out.append(f"  {fmt_date(c.performed_at)}: {c.modality} {fmt_num(c.duration_min)} мин, зоны {zones}, "
                               f"{fmt_num(c.calories)} ккал, У/Б/Ж {fmt_num(c.carbs_pct)}/{fmt_num(c.protein_pct)}/"
                               f"{fmt_num(c.fat_pct)}; {extra}" + (f" — «{c.comment}»" if c.comment else ""))
        if include_body_metrics:
            q = select(BodyMetric)
            if since:
                q = q.where(BodyMetric.measured_at >= since)
            rows = db.scalars(q.order_by(BodyMetric.measured_at)).all()
            if rows:
                out.append("Body metrics:")
                out += [f"  {fmt_date(m.measured_at)}: {fmt_num(m.weight_kg)} кг, мышцы {fmt_num(m.muscle_kg)} кг, "
                        f"жир {fmt_num(m.fat_pct)}%" for m in rows]
        return "\n".join(out) or "No matching history."

    async def t_list_exercises(self, split_name=None, muscle_groups=None, include_archived=True) -> str:
        if split_name:
            split = find_split(self.db, split_name)
            if not split:
                return f"Error: no split named {split_name}"
            return split_context(self.db, split, include_archived=include_archived)
        groups = muscle_groups or MUSCLE_GROUPS
        rows = []
        for e in self.db.scalars(select(Exercise).order_by(Exercise.name)).all():
            if overlaps(e.muscle_groups, groups) and (include_archived or e.status == "active"):
                rows.append(f"- {e.name} [{', '.join(e.muscle_groups)}] ({e.status}); note: {e.rolling_note or '—'}")
        return "\n".join(rows) or "No exercises."

    async def t_show_profile(self, sections=None) -> str:
        db = self.db
        sections = set(sections or ["profile", "injuries", "metrics", "splits", "exercises", "cardio", "behavior"])
        p = db.get(Profile, 1)
        out = []
        if "profile" in sections:
            out += ["ПРОФИЛЬ", "Цели: " + "; ".join(p.goals), f"Структура тренировки: {p.training_structure}",
                    f"Общее состояние: {p.general_state or '—'}"]
        if "behavior" in sections:
            out += ["ПОВЕДЕНИЕ", f"Тон: {p.tone}", f"Напористость: {p.assertiveness}", f"Подтверждения: {p.footnote_style}"]
        if "injuries" in sections:
            out.append("ТРАВМЫ")
            for i in db.scalars(select(Injury).order_by(Injury.id)).all():
                out.append(f"#{i.id} {i.description} — {i.status}; область {i.body_area or '—'}; группы "
                           f"{groups_ru(i.muscle_groups)}; c {fmt_date(i.created_at)}"
                           + (f", закрыта {fmt_date(i.resolved_at)}" if i.resolved_at else "")
                           + (f"; временные замены: {', '.join(i.temporary_substitutions)}" if i.temporary_substitutions else ""))
        if "metrics" in sections:
            out.append("ЗАМЕРЫ (последние 10)")
            for m in db.scalars(select(BodyMetric).order_by(BodyMetric.measured_at.desc()).limit(10)).all():
                out.append(f"{fmt_date(m.measured_at)}: {fmt_num(m.weight_kg)} кг, мышцы {fmt_num(m.muscle_kg)} кг, жир {fmt_num(m.fat_pct)}%")
        if "cardio" in sections:
            baseline = ", ".join(f"{i.get('label', '')} {fmt_num(i.get('incline_pct'))}%/{fmt_num(i.get('speed_kmh'))}" for i in p.treadmill_baseline)
            out += ["КАРДИО", f"Модальность по умолчанию: {p.default_cardio_modality}", f"Базовый профиль дорожки: {baseline or '—'}",
                    f"Тренд дорожки: {p.treadmill_note or '—'}", f"Кардио-форма: {p.cardio_note or '—'}"]
        if "splits" in sections:
            out.append("СПЛИТЫ")
            for s in db.scalars(select(Split).order_by(Split.id)).all():
                out.append(f"{s.name} ({s.status}) — группы: {groups_ru(s.claimed_groups)}\n  специализация: {s.specialization}"
                           f"\n  структура: {s.structure_override or 'глобальная'}\n  заметка: {s.rolling_note or '—'}")
        if "exercises" in sections:
            out.append("УПРАЖНЕНИЯ")
            for e in db.scalars(select(Exercise).order_by(Exercise.status, Exercise.name)).all():
                out.append(f"{e.name} ({'активное' if e.status == 'active' else 'архив'}) — {groups_ru(e.muscle_groups)}\n  заметка: {e.rolling_note or '—'}")
        return "\n".join(out)

    # ---- write tools ----

    async def t_start_session(self, split_name: str) -> str:
        split = find_split(self.db, split_name)
        if not split:
            names = ", ".join(s.name for s in self.db.scalars(select(Split)).all())
            return f"Error: no split named '{split_name}'. Existing: {names}. Ask the client or create one via update_split."
        s = self.session
        if s is None:
            t = now()
            s = TrainingSession(started_at=t, last_activity_at=t, status="open", primary_split_id=split.id,
                                transcript=list(self.transcript_seed), applied_changes=[])
            self.db.add(s)
            self.db.flush()
        else:
            s.primary_split_id = split.id
        return split_context(self.db, split)

    async def t_finalize_plan(self, exercises: list[dict], cardio: dict | None = None) -> str:
        s = self.session
        if s is None:
            return "Error: no open session - call start_session first."
        for old in s.plans:
            if old.status == "accepted":
                old.status = "abandoned"  # superseded by the re-finalized plan
        plan = Plan(status="accepted", finalized_at=now(), cardio_plan=cardio or {})
        s.plans.append(plan)
        self.db.flush()
        created = []
        for pos, item in enumerate(exercises):
            ex, is_new = await self.get_or_create_exercise(item["name"], item.get("muscle_groups"), "active")
            if is_new:
                created.append(f"{ex.name} → {groups_ru(ex.muscle_groups)}")
            plan.items.append(PlanItem(exercise=ex, position=pos,
                                 target_weight=item.get("weight_kg"), target_reps=item.get("reps"),
                                 target_sets=item.get("sets"), superset_group=item.get("superset_group", "") or "",
                                 note=item.get("note", "") or ""))
        self.show_keyboard = True
        note = f"план зафиксирован ({len(exercises)} упр.)"
        if created:
            note += "; новые упражнения: " + "; ".join(created)
        self._note(note)
        return "Plan saved; logging buttons will be shown under your reply."

    async def t_add_substitution(self, name: str, replaces: str | None = None, muscle_groups=None,
                                 weight_kg=None, reps=None, sets=None, comment: str = "") -> str:
        s = self.session
        plan = s.plans[-1] if s and s.plans else None
        if plan is None:
            return "Error: no finalized plan in this session."
        original = None
        if replaces:
            target = find_exercise(self.db, replaces)
            original = next((i for i in plan.items if target and i.exercise_id == target.id), None)
        ex, is_new = await self.get_or_create_exercise(name, muscle_groups, "archived")
        item = PlanItem(exercise=ex, position=len(plan.items),
                        target_weight=weight_kg, target_reps=reps, target_sets=sets,
                        substitutes_item_id=original.id if original else None)
        plan.items.append(item)
        self.db.flush()
        if original is not None:
            original.status = "not_done"
        text = f"замена: {ex.name}" + (f" вместо {original.exercise.name}" if original else " (доп. упражнение)")
        if is_new:
            text += f"; новое упражнение (в архиве, не в ротации) → {groups_ru(ex.muscle_groups)}"
        if weight_kg is not None and reps is not None:
            self.db.add(Performance(session_id=s.id, exercise_id=ex.id, plan_item_id=item.id, performed_at=now(),
                                    weight_kg=weight_kg, reps=reps, sets=sets, comment=comment or ""))
            item.status = "done"
            text += f"; записал {fmt_num(weight_kg)}×{fmt_num(reps)}×{fmt_num(sets)}"
        self.show_keyboard = True
        self._note(text)
        return "Substitution saved; buttons refreshed."

    async def t_log_cardio(self, modality: str, duration_min=None, zone_seconds=None, calories=None, carbs_pct=None,
                           protein_pct=None, fat_pct=None, intervals=None, elliptical_level=None, comment: str = "") -> str:
        s = self.session
        existing = self.db.scalar(select(CardioRecord).where(CardioRecord.session_id == s.id)) if s else None
        rec = existing or CardioRecord(session_id=s.id if s else None, performed_at=now())
        rec.modality = modality
        rec.duration_min = duration_min
        rec.zone_seconds = list(zone_seconds or [])[:5]
        rec.calories, rec.carbs_pct, rec.protein_pct, rec.fat_pct = calories, carbs_pct, protein_pct, fat_pct
        rec.treadmill_intervals = list(intervals or []) if modality == "treadmill" else []
        rec.elliptical_level = elliptical_level if modality == "elliptical" else None
        rec.comment = comment or ""
        if existing is None:
            self.db.add(rec)
        zones = "/".join(f"{z // 60}:{z % 60:02d}" for z in rec.zone_seconds) or "—"
        extra = (", ".join(f"{fmt_num(i.get('incline_pct'))}%/{fmt_num(i.get('speed_kmh'))}" for i in rec.treadmill_intervals)
                 if modality == "treadmill" else f"уровень {fmt_num(elliptical_level)}")
        modality_ru = "дорожка" if modality == "treadmill" else "орбитрек"
        self._note(f"кардио: {modality_ru} {fmt_num(duration_min)} мин, зоны {zones}, {fmt_num(calories)} ккал, "
                   f"У/Б/Ж {fmt_num(carbs_pct)}/{fmt_num(protein_pct)}/{fmt_num(fat_pct)}, {extra}")
        return "Cardio saved."

    async def t_close_session(self) -> str:
        if self.session is None:
            return "No open session."
        self.close_requested = True
        return "Session will be closed and synthesized after your reply. Keep the reply short."

    async def t_log_body_metric(self, weight_kg=None, muscle_kg=None, fat_pct=None, date=None) -> str:
        when = _parse_date(date) or now()
        self.db.add(BodyMetric(measured_at=when, weight_kg=weight_kg, muscle_kg=muscle_kg, fat_pct=fat_pct))
        parts = [f"вес {fmt_num(weight_kg)} кг" if weight_kg is not None else "",
                 f"мышцы {fmt_num(muscle_kg)} кг" if muscle_kg is not None else "",
                 f"жир {fmt_num(fat_pct)}%" if fat_pct is not None else ""]
        self._note(f"замер {fmt_date(when)}: " + ", ".join(p for p in parts if p))
        return "Saved."

    async def t_update_goals(self, goals: list[str]) -> str:
        self.db.get(Profile, 1).goals = goals
        self._note("цели: " + "; ".join(goals))
        return "Saved."

    async def t_update_injury(self, action: str, injury_id=None, description=None, body_area=None, muscle_groups=None,
                              temporary_substitution=None, reverts_proposed=None) -> str:
        db, t = self.db, now()
        if action == "create":
            inj = Injury(description=description or "травма", body_area=body_area or "",
                         muscle_groups=muscle_groups or [], status="active", created_at=t, last_mentioned_at=t,
                         temporary_substitutions=[temporary_substitution] if temporary_substitution else [])
            db.add(inj)
            db.flush()
            self._note(f"травма #{inj.id}: {inj.description} ({groups_ru(inj.muscle_groups)})")
            return f"Created injury #{inj.id}."
        inj = db.get(Injury, injury_id) if injury_id else None
        if inj is None:
            return "Error: injury_id required (see Active injuries in context)."
        inj.last_mentioned_at = t
        if description:
            inj.description = description
        if body_area:
            inj.body_area = body_area
        if muscle_groups:
            inj.muscle_groups = muscle_groups
        if temporary_substitution:
            inj.temporary_substitutions = [*inj.temporary_substitutions, temporary_substitution]
        if reverts_proposed is not None:
            inj.reverts_proposed = reverts_proposed
        if action == "resolve":
            inj.status, inj.resolved_at = "resolved", t
            self._note(f"травма #{inj.id} ({inj.description}) закрыта {fmt_date(t)}")
        elif action == "update" or temporary_substitution:
            self._note(f"травма #{inj.id} обновлена" + (f"; временная замена: {temporary_substitution}" if temporary_substitution else ""))
        return "Saved."  # plain "reinforce" only refreshes last_mentioned_at - no footnote needed

    async def t_update_split(self, action: str, split_name: str, new_name=None, claimed_groups=None,
                             specialization=None, structure_override=None) -> str:
        db = self.db
        split = find_split(db, split_name)
        if action == "create":
            if split:
                return f"Error: split '{split_name}' already exists."
            if not claimed_groups:
                return "Error: a new split needs claimed_groups in the same step."
            split = Split(name=split_name.strip(), claimed_groups=claimed_groups, specialization=specialization or "",
                          structure_override=structure_override or "", status="active")
            db.add(split)
            self._note(f"новый сплит «{split.name}»: {groups_ru(claimed_groups)}")
            return "Created."
        if split is None:
            return f"Error: no split '{split_name}'."
        changes = []
        if new_name:
            split.name = new_name.strip()
            changes.append(f"переименован в «{split.name}»")
        if claimed_groups is not None:
            split.claimed_groups = claimed_groups
            changes.append(f"группы: {groups_ru(claimed_groups)}")
        if specialization is not None:
            split.specialization = specialization
            changes.append("специализация обновлена")
        if structure_override is not None:
            split.structure_override = structure_override
            changes.append(f"структура: {structure_override or 'как глобальная'}")
        if action in ("archive", "restore"):
            split.status = "archived" if action == "archive" else "active"
            changes.append("в архиве" if action == "archive" else "снова активен")
        self._note(f"сплит «{split.name}»: " + ", ".join(changes))
        return "Saved."

    async def t_upsert_exercise(self, name: str, new_name=None, muscle_groups=None, status=None) -> str:
        ex = find_exercise(self.db, name)
        if ex is None:
            ex, _ = await self.get_or_create_exercise(name, muscle_groups, status or "active")
            self._note(f"новое упражнение {ex.name} ({'активное' if ex.status == 'active' else 'архив'}) → {groups_ru(ex.muscle_groups)}")
            return "Created."
        changes = []
        if new_name:
            ex.name = new_name.strip()
            changes.append("переименовано")
        if muscle_groups:
            ex.muscle_groups = muscle_groups
            changes.append(f"группы: {groups_ru(muscle_groups)}")
        if status and status != ex.status:
            ex.status = status
            changes.append("в ротации" if status == "active" else "убрано из ротации (архив)")
        if changes:
            self._note(f"{ex.name}: " + ", ".join(changes))
        return "Saved."

    async def t_update_settings(self, **kwargs) -> str:
        p = self.db.get(Profile, 1)
        labels = {"training_structure": "структура тренировки", "tone": "тон", "assertiveness": "напористость",
                  "footnote_style": "стиль подтверждений", "default_cardio_modality": "кардио по умолчанию",
                  "treadmill_baseline": "базовый профиль дорожки"}
        changed = []
        for key, value in kwargs.items():
            if key in labels and value is not None:
                setattr(p, key, value)
                changed.append(labels[key])
        if changed:
            self._note("настройки: " + ", ".join(changed))
        return "Saved."

    async def t_edit_note(self, target: str, text: str, name: str | None = None) -> str:
        db = self.db
        p = db.get(Profile, 1)
        if target in ("general_state", "cardio_note", "treadmill_note"):
            setattr(p, target, text)
        elif target == "split_note":
            split = find_split(db, name or "")
            if not split:
                return "Error: split not found."
            split.rolling_note = text
        else:
            ex = find_exercise(db, name or "")
            if not ex:
                return "Error: exercise not found."
            ex.rolling_note = text
        self._note(f"заметка «{name or target}» исправлена")
        return "Saved."
