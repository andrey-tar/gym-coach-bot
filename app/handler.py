"""Update handling: owner check, dedupe, lazy close, structured logging buttons, model turns."""

import asyncio
import json
import logging
import os
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import llm, telegram
from .config import now, settings
from .context import build_dynamic_context, fmt_date, fmt_num, fmt_perf, fmt_triple, open_session, prev_performance
from .db import db_session
from .llm import LLMUnavailable
from .models import KV, Performance, PlanItem, Profile, ProcessedUpdate, TrainingSession
from .prompts import COACH_SYSTEM, EXTRACTION_SYSTEM, REACT_SYSTEM, SEPARATE_REPLY_ADDENDUM
from .synthesis import close_session, is_stale
from .tools import READ_TOOLS, WRITE_TOOLS, GROUPS_SCHEMA, ToolContext

log = logging.getLogger(__name__)

_lock = asyncio.Lock()  # one update at a time (spec 10: sequential per chat)

HELP_TEXT = (
    "Привет! Я твой тренер. Как пользоваться:\n"
    "• Напиши, что тренируем сегодня (например, «сегодня ноги») — можно с подробностями: как спал, что болит.\n"
    "• Обсуждаем план, спорим. Когда согласны — напиши «давай финальную версию» или /final.\n"
    "• Результаты упражнений — кнопками под планом. Кардио — текстом, как в приложении пульсометра.\n"
    "• В конце — кнопка «🏁 Завершить тренировку» или просто «всё на сегодня».\n"
    "• В любой момент: вес/замеры, вопросы о прогрессе, «покажи профиль» и правки.\n"
    "• /model — какая модель сейчас отвечает; /model sonnet или /model gemini — переключить (запасной вариант, "
    "если у одной кончился баланс или сервис лежит)."
)

MODEL_KEYS = {"gemini": "GEMINI_API_KEY", "sonnet": "ANTHROPIC_API_KEY"}

ENTRY_RE = re.compile(
    r"^\s*(\d+(?:[.,]\d+)?)\s*(?:кг|kg)?\s*[x×х*/\s]\s*(\d+)(?:\s*[x×х*/\s]\s*(\d+))?\b[\s,.;:—-]*(.*)$",
    re.I | re.S,
)


# ---------------------------------------------------------------- helpers

def kv_get(db: Session, key: str, default=None):
    row = db.get(KV, key)
    return row.value if row and row.value is not None else default


def kv_set(db: Session, key: str, value) -> None:
    row = db.get(KV, key)
    if row:
        row.value = value
    else:
        db.add(KV(key=key, value=value))


def loose_buffer(db: Session) -> list[dict]:
    cutoff = (now() - timedelta(minutes=settings.loose_buffer_minutes)).isoformat()
    items = [m for m in kv_get(db, "loose", []) if m.get("ts", "") >= cutoff]
    return items[-settings.loose_buffer_messages:]


def to_api_messages(history: list[dict], user_text: str) -> list[dict]:
    msgs = [{"role": m["role"], "content": m["content"]} for m in history if m.get("content")]
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    msgs.append({"role": "user", "content": user_text})
    return msgs


def append_transcript(session: TrainingSession, role: str, content: str) -> None:
    session.transcript = [*session.transcript, {"role": role, "content": content, "ts": now().isoformat()}]
    session.last_activity_at = now()


def footnote(notes: list[str]) -> str:
    return ("\n\n📝 " + "; ".join(notes)) if notes else ""


def short(name: str, limit: int = 34) -> str:
    return name if len(name) <= limit else name[: limit - 1] + "…"


def latest_plan(session: TrainingSession):
    return session.plans[-1] if session.plans else None


def item_perf(db: Session, item: PlanItem) -> Performance | None:
    return db.scalar(select(Performance).where(Performance.plan_item_id == item.id))


def plan_units(plan) -> list[list[PlanItem]]:
    """Plan items in order, superset partners merged into one logging unit."""
    units, by_group = [], {}
    for item in plan.items:
        if item.status == "not_done":
            continue
        group = item.superset_group
        if group and group in by_group:
            by_group[group].append(item)
            continue
        unit = [item]
        units.append(unit)
        if group:
            by_group[group] = unit
    return units


def target_text(item: PlanItem) -> str:
    return f"{fmt_num(item.target_weight)}×{fmt_num(item.target_reps)}×{fmt_num(item.target_sets)}"


def build_keyboard(db: Session, session: TrainingSession) -> list[list[dict]]:
    rows = []
    plan = latest_plan(session)
    if plan and plan.status == "accepted":
        for unit in plan_units(plan):
            perfs = [item_perf(db, i) for i in unit]
            if len(unit) == 1:
                perf, item = perfs[0], unit[0]
                label = (f"✏️ {short(item.exercise.name, 26)}: {fmt_triple(perf)}" if perf
                         else f"▫️ {short(item.exercise.name)}")
            else:
                names = " + ".join(short(i.exercise.name, 16) for i in unit)
                label = f"{'✏️' if all(perfs) else '▫️'} Суперсет: {names}"
            rows.append([telegram.button(label, {"a": "e", "i": [i.id for i in unit]})])
        if not session.strength_done:
            rows.append([telegram.button("💪 Силовая готова", {"a": "sd"})])
    rows.append([telegram.button("🏁 Завершить тренировку", {"a": "fin"})])
    return rows


async def send_keyboard(db: Session, chat_id: int, session: TrainingSession, text: str) -> None:
    """Sends a fresh logging keyboard and strips the previous one, so only one is live."""
    old = kv_get(db, "kbd")
    msg = await telegram.send_message(chat_id, text, build_keyboard(db, session))
    if old and old.get("msg") != msg.get("message_id"):
        await telegram.edit_keyboard(old["chat"], old["msg"], None)
    kv_set(db, "kbd", {"chat": chat_id, "msg": msg.get("message_id")})


async def clear_keyboard(db: Session) -> None:
    old = kv_get(db, "kbd")
    if old:
        await telegram.edit_keyboard(old["chat"], old["msg"], None)
        kv_set(db, "kbd", None)


def render_plan(db: Session, session: TrainingSession) -> str:
    """Deterministic recap of the locked plan (used by /final)."""
    p = db.get(Profile, 1)
    plan = latest_plan(session)
    lines = [f"Цель — {'; '.join(p.goals).lower()}.", "", f"**Финальный план{f' — {session.primary_split.name}' if session.primary_split else ''}:**"]
    for n, unit in enumerate(plan_units(plan), 1):
        if len(unit) > 1:
            lines.append(f"{n}. **Суперсет:**")
        for item in unit:
            target = " × ".join(x for x in [
                f"{fmt_num(item.target_weight)} кг" if item.target_weight is not None else "",
                str(item.target_reps) if item.target_reps else "", str(item.target_sets) if item.target_sets else ""] if x)
            prev = prev_performance(db, item.exercise_id, session.id)
            prev_txt = f"прошлый раз {fmt_triple(prev)} → " if prev else ""
            note = f" — {item.note}" if item.note else ""
            head = "   • " if len(unit) > 1 else f"{n}. "
            lines.append(f"{head}{item.exercise.name}: {prev_txt}**{target or 'по самочувствию'}**{note}")
    c = plan.cardio_plan or {}
    if c:
        if c.get("modality") == "elliptical":
            extra = f"уровень {fmt_num(c.get('elliptical_level'))}"
            name = "орбитрек"
        else:
            extra = ", ".join(f"{i.get('label', '')} {fmt_num(i.get('incline_pct'))}%/{fmt_num(i.get('speed_kmh'))}"
                              for i in c.get("intervals") or [])
            name = "дорожка"
        lines.append(f"Кардио: {name} {fmt_num(c.get('duration_min'))} мин{', ' + extra if extra else ''}")
    lines.append("\nЛогируй каждое упражнение кнопками ниже.")
    return "\n".join(lines)


# ---------------------------------------------------------------- model turns

async def model_turn(db: Session, user_text: str) -> tuple[str, ToolContext]:
    session = open_session(db)
    history = session.transcript if session else loose_buffer(db)
    messages = to_api_messages(history, user_text)
    seed = [] if session else [*loose_buffer(db)]
    ctx = ToolContext(db, transcript_seed=seed)
    dynamic = build_dynamic_context(db, session)

    if settings.handler_mode == "separate":
        reply = await llm.run_tools_loop(stable_system=COACH_SYSTEM + SEPARATE_REPLY_ADDENDUM, context=dynamic,
                                         messages=messages, tools=READ_TOOLS, executor=ctx.execute)
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in messages[:-1][-10:])
        extraction_user = (f"Earlier in this conversation:\n{convo or '(nothing)'}\n\n"
                           f"Latest client message:\n{user_text}\n\nCoach reply:\n{reply}\n\n"
                           "Persist what this exchange established.")
        await llm.run_tools_loop(stable_system=EXTRACTION_SYSTEM, context=build_dynamic_context(db, open_session(db)), purpose="extraction",
                                 messages=[{"role": "user", "content": extraction_user}], tools=WRITE_TOOLS,
                                 executor=ctx.execute)
    else:
        reply = await llm.run_tools_loop(stable_system=COACH_SYSTEM, context=dynamic, messages=messages,
                                         tools=READ_TOOLS + WRITE_TOOLS, executor=ctx.execute)
    return reply or "…", ctx


async def finish_turn(db: Session, chat_id: int, user_text: str, reply: str, ctx: ToolContext) -> None:
    session = open_session(db)
    full_reply = reply + footnote(ctx.footnotes)
    if session:
        append_transcript(session, "user", user_text)
        append_transcript(session, "assistant", full_reply)
        kv_set(db, "loose", [])
    else:
        buf = loose_buffer(db)
        ts = now().isoformat()
        kv_set(db, "loose", [*buf, {"role": "user", "content": user_text, "ts": ts},
                             {"role": "assistant", "content": full_reply, "ts": ts}])

    if session and ctx.show_keyboard and not ctx.close_requested:
        await send_keyboard(db, chat_id, session, full_reply)
    else:
        await telegram.send_message(chat_id, full_reply)

    if session and ctx.close_requested:
        await do_close(db, chat_id, session)


async def do_close(db: Session, chat_id: int, session: TrainingSession, lazy: bool = False) -> None:
    await telegram.send_typing(chat_id)
    notes, summary = await close_session(db, session)
    await clear_keyboard(db)
    head = (f"Закрыл прошлую тренировку ({fmt_date(session.started_at)}), которую ты не завершил."
            if lazy else "🏁 Тренировка закрыта.")
    if lazy and not notes:
        return  # nothing happened in it; no need to bother the user
    body = f"\n\n{summary}" if summary else ""
    await telegram.send_message(chat_id, head + body + footnote([notes] if notes else []))


PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "plan_proposed": {"type": "boolean", "description": "true if the coach has listed concrete exercises at any "
                          "point, even conditionally or ending with a question"},
        "exercises": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "muscle_groups": GROUPS_SCHEMA,
                    "weight_kg": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                    "reps": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "sets": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
                    "superset_group": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["name", "muscle_groups", "weight_kg", "reps", "sets", "superset_group", "note"],
                "additionalProperties": False,
            },
        },
        "cardio_modality": {"anyOf": [{"type": "string", "enum": ["treadmill", "elliptical"]}, {"type": "null"}]},
        "cardio_duration_min": {"anyOf": [{"type": "number"}, {"type": "null"}]},
    },
    "required": ["plan_proposed", "exercises", "cardio_modality", "cardio_duration_min"],
    "additionalProperties": False,
}


async def finalize_command(db: Session, chat_id: int) -> None:
    """/final: deterministic plan lock - structured extraction of the latest agreed plan, rendered by code."""
    session = open_session(db)
    if session is None or not session.transcript:
        await telegram.send_message(chat_id, "Пока нечего фиксировать — сначала обсудим план. Что тренируем сегодня?")
        return
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in session.transcript)
    result = await llm.structured(
        stable_system=("The client sent /final, which means they ACCEPT the coach's latest proposed workout plan, "
                       "including every change agreed during the negotiation. A proposal phrased conditionally or ending "
                       "with a question still counts - /final is the client's answer. Extract that plan. Set "
                       "plan_proposed=false only if the coach has not listed any concrete exercises yet. Use exact exercise names from the "
                       "registry when they match. muscle_groups: target groups of each exercise."),
        context=build_dynamic_context(db, session), user=convo, schema=PLAN_SCHEMA, purpose="final",
        effort=settings.llm_effort,
    )
    if not result["plan_proposed"] or not result["exercises"]:
        await telegram.send_message(chat_id, "Не вижу в разговоре конкретного плана. Давай сначала его составим.")
        return
    ctx = ToolContext(db)
    cardio = {"modality": result["cardio_modality"], "duration_min": result["cardio_duration_min"]} if result["cardio_modality"] else None
    if cardio and cardio["modality"] == "treadmill":
        cardio["intervals"] = db.get(Profile, 1).treadmill_baseline
    await ctx.t_finalize_plan(
        [{k: v for k, v in ex.items() if v not in (None, "")} for ex in result["exercises"]], cardio)
    db.flush()
    db.refresh(session)
    text = render_plan(db, session) + footnote(ctx.footnotes)
    append_transcript(session, "user", "/final")
    append_transcript(session, "assistant", text)
    await send_keyboard(db, chat_id, session, text)


# ---------------------------------------------------------------- structured logging

def save_entry(db: Session, session: TrainingSession, item: PlanItem, weight, reps, sets, comment: str) -> Performance:
    perf = item_perf(db, item)  # re-entry = the "Fix" path: overwrite until session close
    if perf is None:
        perf = Performance(session_id=session.id, exercise_id=item.exercise_id, plan_item_id=item.id, performed_at=now())
        db.add(perf)
    perf.weight_kg, perf.reps, perf.sets = weight, reps, sets
    if comment:
        perf.comment = comment
    item.status = "done"
    append_transcript(session, "user", f"[записано кнопками] {item.exercise.name}: {fmt_num(weight)} кг × {reps} × {sets}"
                      + (f" — {comment}" if comment else ""))
    return perf


def pending_items(db: Session, pending: dict | None) -> list[PlanItem]:
    if not pending:
        return []
    ids = pending.get("items") or ([pending["item"]] if pending.get("item") else [])
    return [i for i in (db.get(PlanItem, x) for x in ids) if i is not None]


SKIP_COMMENT = [[telegram.button("Без комментария", {"a": "x"})]]
COMMENT_ASK = "Как шло? Комментарий одним сообщением — или пропусти."


async def react(db: Session, session: TrainingSession, items: list[PlanItem]) -> str:
    """Short coach reaction to a freshly logged exercise/superset; '' if the model call fails."""
    p = db.get(Profile, 1)
    rows = []
    for item in items:
        perf = item_perf(db, item)
        rows.append(f"- {item.exercise.name}: target {target_text(item)}; logged {fmt_perf(perf) if perf else '—'}; "
                    f"previous session {fmt_triple(prev_performance(db, item.exercise_id, session.id))}; "
                    f"exercise note: {item.exercise.rolling_note or '—'}")
    talk = [f"{m['role']}: {m['content'][:300]}" for m in session.transcript[-8:]]
    user = ("Logged now:\n" + "\n".join(rows) + f"\n\nClient general state: {p.general_state or '—'}"
            "\n\nRecent conversation:\n" + "\n".join(talk))
    try:
        result = await llm.structured(stable_system=REACT_SYSTEM, user=user, effort="low", max_tokens=4000,
                                      purpose="react", schema={"type": "object", "properties": {"comment": {"type": "string"}},
                                                               "required": ["comment"], "additionalProperties": False})
        return result["comment"].strip()
    except Exception:
        log.exception("reaction failed")
        return ""


async def finish_entry(db: Session, chat_id: int, session: TrainingSession, items: list[PlanItem]) -> None:
    """Logging of a unit is complete: coach reaction, back to the keyboard."""
    session.pending_entry = None
    await telegram.send_typing(chat_id)
    reaction = await react(db, session, items)
    logged = "\n".join(f"✅ {i.exercise.name}: {fmt_triple(item_perf(db, i))}" for i in items)
    if reaction:
        append_transcript(session, "assistant", reaction)
    await send_keyboard(db, chat_id, session, logged + (f"\n\n{reaction}" if reaction else "\n\nСледующее?"))


def parse_entry(text: str):
    m = ENTRY_RE.match(text)
    if not m:
        return None
    sets = int(m.group(3)) if m.group(3) else None
    return float(m.group(1).replace(",", ".")), int(m.group(2)), sets, (m.group(4) or "").strip()


async def handle_pending(db: Session, chat_id: int, session: TrainingSession, text: str) -> bool:
    """Returns True if the text was consumed as structured input."""
    pending = session.pending_entry or {}
    items = pending_items(db, pending)
    if not items:
        return False
    if pending.get("stage") == "comment":
        for item in items:
            perf = item_perf(db, item)
            if perf:
                perf.comment = text.strip()
        names = " + ".join(i.exercise.name for i in items)
        append_transcript(session, "user", f"[комментарий к {names}] {text.strip()}")
        await finish_entry(db, chat_id, session, items)
        return True

    extra_comment = ""
    if len(items) == 1:
        entries = [parse_entry(text)]
        if entries[0] is None:
            session.pending_entry = None
            return False
    else:
        lines = [line for line in re.split(r"[\n;]+", text) if line.strip()]
        entries, rest = [], []
        for line in lines:
            parsed = parse_entry(line) if len(entries) < len(items) else None
            if parsed:
                entries.append(parsed)
            else:
                rest.append(line.strip())
        extra_comment = " ".join(rest)
        if len(entries) < len(items):
            await telegram.send_message(
                chat_id, f"Нужно {len(items)} строки с цифрами — по одной на упражнение, в порядке суперсета. "
                         "Попробуй ещё раз или нажми «Отмена».", [[telegram.button("Отмена", {"a": "c"})]])
            return True

    any_comment = bool(extra_comment)
    for item, (weight, reps, sets, comment) in zip(items, entries):
        comment = " ".join(x for x in (comment, extra_comment) if x)
        any_comment = any_comment or bool(comment)
        save_entry(db, session, item, weight, reps, sets or item.target_sets or 3, comment)
    if any_comment:
        await finish_entry(db, chat_id, session, items)
    else:
        session.pending_entry = {"items": [i.id for i in items], "stage": "comment"}
        logged = "\n".join(f"✅ {i.exercise.name}: {fmt_triple(item_perf(db, i))}" for i in items)
        await telegram.send_message(chat_id, f"{logged}\n{COMMENT_ASK}", SKIP_COMMENT)
    return True


async def handle_callback(db: Session, chat_id: int, cq: dict) -> None:
    await telegram.answer_callback(cq["id"])
    try:
        data = json.loads(cq.get("data") or "{}")
    except json.JSONDecodeError:
        return
    session = open_session(db)
    if session is None:
        await telegram.send_message(chat_id, "Эта тренировка уже закрыта.")
        return
    session.last_activity_at = now()
    action = data.get("a")

    if action in ("e", "asp"):
        ids = data.get("i")
        ids = ids if isinstance(ids, list) else [ids]
        items = [i for i in (db.get(PlanItem, x) for x in ids) if i is not None and i.plan.session_id == session.id]
        if not items:
            return
        if action == "asp":
            for item in items:
                save_entry(db, session, item, item.target_weight, item.target_reps, item.target_sets, "")
            session.pending_entry = {"items": [i.id for i in items], "stage": "comment"}
            await telegram.send_message(chat_id, f"✅ Как в плане.\n{COMMENT_ASK}", SKIP_COMMENT)
            return
        session.pending_entry = {"items": [i.id for i in items], "stage": "numbers"}
        perfs = [item_perf(db, i) for i in items]
        if len(items) == 1:
            item, perf = items[0], perfs[0]
            prev = prev_performance(db, item.exercise_id, session.id)
            text = (f"**{item.exercise.name}**\nПлан: {target_text(item)}"
                    + (f" · прошлый раз: {fmt_triple(prev)}" if prev else "") + "\n")
            if perf:
                text += f"Сейчас записано: {fmt_triple(perf)} — пришли новые цифры, чтобы исправить.\n"
            text += "Пришли: вес повторы подходы [комментарий]\nнапример: 45 12 3 последний подход тяжело"
        else:
            text = "**Суперсет**\n"
            for n, (item, perf) in enumerate(zip(items, perfs), 1):
                prev = prev_performance(db, item.exercise_id, session.id)
                text += (f"{n}. {item.exercise.name}: план {target_text(item)}"
                         + (f" · прошлый раз {fmt_triple(prev)}" if prev else "")
                         + (f" · записано {fmt_triple(perf)}" if perf else "") + "\n")
            text += ("Пришли по строке на каждое упражнение, в том же порядке:\nвес повторы подходы [комментарий]\n"
                     "например:\n9 12 3\n10 10 3 последний подход тяжело")
        buttons = [[telegram.button("Отмена", {"a": "c"})]]
        if all(i.target_weight is not None and i.target_reps for i in items) and not any(perfs):
            buttons[0].insert(0, telegram.button("✅ Как в плане", {"a": "asp", "i": [i.id for i in items]}))
        await telegram.send_message(chat_id, text, buttons)
    elif action in ("x", "c"):
        pending = session.pending_entry or {}
        items = pending_items(db, pending)
        if action == "x" and pending.get("stage") == "comment" and items:
            await finish_entry(db, chat_id, session, items)  # comment skipped
            return
        session.pending_entry = None
        await send_keyboard(db, chat_id, session, "Ок. Следующее?")
    elif action == "sd":
        session.strength_done = True
        session.pending_entry = None
        append_transcript(session, "user", "[силовая часть завершена]")
        await send_keyboard(db, chat_id, session,
                            "💪 Силовая записана. После кардио пришли данные с пульсометра: длительность, время в зонах 1–5, "
                            "калории, % углеводов/белков/жиров, профиль дорожки (наклон/скорость) или уровень орбитрека — "
                            "и как разгонялся пульс.")
    elif action == "fin":
        await do_close(db, chat_id, session)


# ---------------------------------------------------------------- entry point

async def handle_update(update: dict) -> None:
    async with _lock:
        msg = update.get("message")
        cq = update.get("callback_query")
        sender = (msg or cq or {}).get("from", {}).get("id")
        chat_id = (msg or {}).get("chat", {}).get("id") or (cq or {}).get("message", {}).get("chat", {}).get("id")
        if sender != settings.owner_id or chat_id is None:
            log.warning("ignored update from %s", sender)
            return

        try:
            with db_session() as db:
                db.add(ProcessedUpdate(update_id=update["update_id"], processed_at=now()))
                db.flush()
        except IntegrityError:
            log.info("duplicate update %s skipped", update["update_id"])
            return

        try:
            with db_session() as db:
                # Lazy close (spec 3.1 step 10) - always before handling the new input.
                stale = open_session(db)
                if stale and is_stale(db, stale):
                    await do_close(db, chat_id, stale, lazy=True)
            with db_session() as db:
                if cq:
                    await handle_callback(db, chat_id, cq)
                elif msg and msg.get("text"):
                    await handle_text(db, chat_id, msg["text"])
                elif msg:
                    await telegram.send_message(chat_id, "Я понимаю только текст 🙂")
        except LLMUnavailable as e:
            other = next((k for k, m in llm.SWITCHABLE.items() if m != llm.model_name()), None)
            hint = f"\n\nЕсли не проходит — переключись на запасную модель: /model {other}" if other else ""
            await telegram.send_message(chat_id, f"⚠️ {e}{hint}")
        except Exception:
            log.exception("update failed")
            await telegram.send_message(chat_id, "⚠️ Что-то сломалось при обработке сообщения. Ничего не сохранено — попробуй ещё раз.")


async def handle_text(db: Session, chat_id: int, text: str) -> None:
    command = text.strip().split()[0].lower() if text.strip() else ""
    if command in ("/start", "/help"):
        await telegram.send_message(chat_id, HELP_TEXT)
        return
    typing = asyncio.create_task(keep_typing(chat_id))
    try:
        if command == "/final":
            await finalize_command(db, chat_id)
            return
        if command == "/model":
            await model_command(db, chat_id, text)
            return
        session = open_session(db)
        if session and session.pending_entry and not text.startswith("/"):
            if await handle_pending(db, chat_id, session, text):
                return
        reply, ctx = await model_turn(db, text)
        typing.cancel()
        await finish_turn(db, chat_id, text, reply, ctx)
    finally:
        typing.cancel()


async def model_command(db: Session, chat_id: int, text: str) -> None:
    """/model shows the answering model; /model <gemini|sonnet> switches it (stored, survives restarts)."""
    parts = text.split()
    current = llm.model_name()
    if len(parts) < 2:
        options = ", ".join(f"/model {k}" for k, m in llm.SWITCHABLE.items() if m != current)
        await telegram.send_message(chat_id, f"Сейчас отвечает: {current}.\nПереключить: {options}")
        return
    choice = parts[1].lower()
    if choice not in llm.SWITCHABLE:
        await telegram.send_message(chat_id, f"Не знаю такую модель. Варианты: {', '.join(llm.SWITCHABLE)}")
        return
    if not os.getenv(MODEL_KEYS[choice]):
        await telegram.send_message(chat_id, f"⚠️ Для {choice} не задан ключ {MODEL_KEYS[choice]} — переключить не могу.")
        return
    llm.set_active_model(db, llm.SWITCHABLE[choice])
    await telegram.send_message(chat_id, f"Готово: теперь отвечает {llm.SWITCHABLE[choice]} (было {current}). "
                                         "Открытая тренировка продолжается как обычно.")


async def keep_typing(chat_id: int) -> None:
    while True:
        await telegram.send_typing(chat_id)
        await asyncio.sleep(4.5)
