"""Scripted model comparison (spec Section 7).

Runs the same scripted exchanges through the real handler for each model, on a copy of a
snapshot database (never the live one), with Telegram stubbed out. Produces:
  - objective checks: did the right state get written?
  - an HTML report with both models' replies side by side for the subjective read
  - cost / latency per model from the usage log

    python -m scripts.eval_models --models claude-sonnet-5 gemini-3.8-flash
    python -m scripts.eval_models --models claude-sonnet-5 --only plan_negotiation
"""

import argparse
import asyncio
import html
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "data" / "after_import.db"
OWNER = 777  # fake owner id inside the eval

LEG_CARDIO = ("Кардио: дорожка 60 мин, гора 8%/5.5, предгорье 4%/6.5. Зоны: 1 — 20:10, 2 — 25:00, 3 — 14:50, "
              "4 — 0:00, 5 — 0:00. 950 ккал, углеводы 50%, белки 3%, жиры 47%. Пульс разгонялся тяжело.")

# Steps: ("say", text) | ("log", exercise-name substring, "w r s comment") | ("btn", "sd"|"fin")
SCENARIOS = {
    "plan_negotiation": {
        "about": "Plan proposal, pushback on an ego weight, incidental swap, finalize by phrase.",
        "steps": [
            ("say", "Сегодня ноги. Спал нормально, часов 7, икры немного забиты после прошлой тренировки."),
            ("say", "Давай жим ногами сразу 130 кг, чувствую силу."),
            ("say", "Ладно, убедил. Но вместо разгибаний ног сегодня давай выпады с гантелями."),
            ("say", "Ок, согласен, давай финальную версию."),
        ],
    },
    "final_command": {
        "about": "/final deterministic lock after a short negotiation.",
        "steps": [("say", "Сегодня ноги, давай как обычно."), ("say", "/final")],
    },
    "substitution": {
        "about": "On-the-spot substitution (machine busy) gets its own structured entry; session close.",
        "steps": [
            ("say", "Сегодня ноги, давай план."),
            ("say", "/final"),
            ("log", "Жим ногами", "100 10 3 легко, с запасом"),
            ("say", "Тренажёр для сгибаний ног занят намертво, сделал вместо него румынскую тягу с гантелями: "
                    "по 20 кг в руке, 12 повторов, 3 подхода."),
            ("btn", "fin"),
        ],
    },
    "injury": {
        "about": "Injury mentioned at session start -> injury record with groups, plan adapted.",
        "steps": [
            ("say", "Сегодня ноги, но правое колено опять ноет, особенно при сгибании под нагрузкой."),
            ("say", "Ок, давай финальную версию."),
        ],
    },
    "explicit_vs_incidental": {
        "about": "Emotional outburst must NOT archive; explicit permanent statement MUST archive.",
        "steps": [
            ("say", "Сегодня ноги."),
            ("say", "/final"),
            ("log", "носки", "0 15 3 жжёт"),
            ("say", "Ненавижу эти подъёмы на носки, никогда больше их делать не буду!!"),
            ("say", "А вот серьёзно, решение: пресс в тренажёре убираем из ротации насовсем, пресс буду качать дома."),
            ("btn", "fin"),
        ],
    },
    "history_analysis": {
        "about": "'Is today's dip fair?' should query raw history and give a grounded verdict.",
        "steps": [
            ("say", "Сегодня ноги."),
            ("say", "/final"),
            ("log", "Жим ногами", "90 8 3 тяжело шло"),
            ("say", "Мне не нравится, как сегодня шёл жим ногами. Это справедливо или я сдаю?"),
        ],
    },
    "multi_field_close": {
        "about": "Cardio report parsing + close touching several fields at once.",
        "steps": [
            ("say", "Сегодня ноги."),
            ("say", "/final"),
            ("log", "Жим ногами", "100 10 3 нормально"),
            ("log", "Сгибания ног", "46 10 3"),
            ("btn", "sd"),
            ("say", LEG_CARDIO),
            ("say", "Всё, на сегодня закончил. Кстати, утром весы: 84.2 кг, мышцы 38.4 кг, жир 21.5%. "
                    "И добавь цель: к лету подтягиваться 10 раз."),
        ],
    },
    "noise_discounting": {
        "about": "Rested-strong / poor-sleep-weak / normal: note and next proposal must anchor to the normal trend.",
        "steps": [
            ("say", "Сегодня ноги."), ("say", "/final"),
            ("log", "Жим ногами", "105 10 3 после недели отдыха — очень легко, свежий"), ("btn", "fin"),
            ("note", "Жим ногами"), ("days", 7),
            ("say", "Сегодня ноги."), ("say", "/final"),
            ("log", "Жим ногами", "90 8 3 спал 4 часа и не поел, еле-еле"), ("btn", "fin"),
            ("note", "Жим ногами"), ("days", 7),
            ("say", "Сегодня ноги."), ("say", "/final"),
            ("log", "Жим ногами", "102.5 10 3 нормально, рабочий вес"), ("btn", "fin"),
            ("note", "Жим ногами"), ("days", 7),
            ("say", "Сегодня ноги. Что по жиму ногами предлагаешь и почему?"),
        ],
    },
    "standalone": {
        "about": "Out-of-session messages: body metric, show profile, goal edit - no session opened.",
        "steps": [
            ("say", "вес 83.9"),
            ("say", "Покажи, что у тебя записано по ногам: упражнения и заметки."),
            ("say", "Поменяй цель: основная — рекомпозиция, жир вниз при сохранении силы."),
        ],
    },
}


def checks_for(name: str, db) -> list[tuple[str, bool]]:
    from sqlalchemy import select

    from app.models import BodyMetric, CardioRecord, Exercise, Injury, Plan, PlanItem, Profile, TrainingSession

    sessions = db.scalars(select(TrainingSession).where(TrainingSession.imported.is_(False))).all()
    plans = [p for s in sessions for p in s.plans]
    ex = {e.name: e for e in db.scalars(select(Exercise)).all()}

    def find(sub):
        return next((e for n, e in ex.items() if sub.lower() in n.lower()), None)

    if name == "plan_negotiation":
        lunge = find("выпад")
        ext = find("Разгибания ног")
        return [("session opened under Ноги", any(s.primary_split and s.primary_split.name == "Ноги" for s in sessions)),
                ("plan finalized", any(p.status == "accepted" for p in plans)),
                ("lunge exercise in plan", bool(lunge) and any(i.exercise_id == lunge.id for p in plans for i in p.items)),
                ("lunge tagged with quads/glutes", bool(lunge) and {"quads", "glutes"} & set(lunge.muscle_groups) != set()),
                ("leg extension NOT archived (incidental swap)", bool(ext) and ext.status == "active"),
                ("no 130 kg leg press in plan", not any(i.target_weight and i.target_weight >= 130 for p in plans for i in p.items))]
    if name == "final_command":
        return [("plan finalized with >=3 exercises", any(p.status == "accepted" and len(p.items) >= 3 for p in plans))]
    if name == "substitution":
        rdl = next((e for n, e in ex.items() if "румын" in n.lower() or "rdl" in n.lower()), None)
        items = [i for p in plans for i in p.items]
        curl = find("Сгибания ног")
        from app.models import Performance
        perf = db.scalar(select(Performance).where(Performance.exercise_id == rdl.id)) if rdl else None
        return [("substitute exercise created", bool(rdl)),
                ("substitute archived (not in rotation)", bool(rdl) and rdl.status == "archived"),
                ("substitute has its own logged result", perf is not None and perf.reps == 12),
                ("planned leg curl marked not done", any(i.exercise_id == (curl.id if curl else -1) and i.status == "not_done" for i in items)),
                ("plan fulfilled + session closed", any(p.status == "fulfilled" for p in plans) and all(s.status == "closed" for s in sessions))]
    if name == "injury":
        inj = db.scalars(select(Injury).where(Injury.status == "active")).all()
        return [("active knee injury recorded", any("колен" in (i.description + i.body_area).lower() for i in inj)),
                ("injury has leg muscle groups", any({"quads", "hamstrings"} & set(i.muscle_groups) for i in inj))]
    if name == "explicit_vs_incidental":
        calves, abs_ = find("носки"), find("Пресс")
        return [("calf raises still active (outburst ignored)", bool(calves) and calves.status == "active"),
                ("ab machine archived (explicit)", bool(abs_) and abs_.status == "archived")]
    if name == "history_analysis":
        return []  # verdict quality is judged by reading; tool use is shown in the report
    if name == "multi_field_close":
        c = db.scalar(select(CardioRecord).where(CardioRecord.session_id.in_([s.id for s in sessions])))
        bm = db.scalars(select(BodyMetric)).all()
        goals = db.get(Profile, 1).goals
        return [("cardio logged as treadmill", bool(c) and c.modality == "treadmill"),
                ("zones parsed", bool(c) and c.zone_seconds[:3] == [1210, 1500, 890]),
                ("calories + macros", bool(c) and c.calories == 950 and c.carbs_pct == 50 and c.fat_pct == 47),
                ("treadmill intervals 8/5.5 + 4/6.5", bool(c) and len(c.treadmill_intervals) == 2),
                ("body metric 84.2 logged", any(m.weight_kg == 84.2 for m in bm)),
                ("goal about pull-ups added", any("подтяг" in g.lower() for g in goals)),
                ("fat-loss goal kept", any("жир" in g.lower() for g in goals)),
                ("session closed", all(s.status == "closed" for s in sessions))]
    if name == "noise_discounting":
        return []  # notes after each session are shown in the report
    if name == "standalone":
        goals = db.get(Profile, 1).goals
        return [("no training session opened", not sessions),
                ("body metric 83.9 logged", any(m.weight_kg == 83.9 for m in db.scalars(select(BodyMetric)).all())),
                ("goal changed to recomposition", any("рекомпоз" in g.lower() for g in goals))]
    return []


# ---------------------------------------------------------------- worker (one scenario, one model)

async def run_worker(scenario: str, out_path: Path) -> None:
    from sqlalchemy import select

    from app import handler, telegram
    from app.db import db_session
    from app.models import Exercise
    from app.context import open_session
    from app.tools import ToolContext

    events: list[dict] = []
    counter = {"msg": 0, "update": 0}

    async def fake_send(chat_id, text, keyboard=None):
        counter["msg"] += 1
        buttons = [b["text"] for row in (keyboard or []) for b in row]
        events.append({"type": "bot", "text": text, "buttons": buttons})
        return {"message_id": counter["msg"]}

    async def noop(*a, **k):
        return None

    telegram.send_message = fake_send
    telegram.edit_keyboard = noop
    telegram.answer_callback = noop
    telegram.send_typing = noop
    telegram.send_document = noop

    original_execute = ToolContext.execute

    async def logged_execute(self, name, args):
        events.append({"type": "tool", "name": name, "args": args})
        return await original_execute(self, name, args)

    ToolContext.execute = logged_execute

    def update(**payload):
        counter["update"] += 1
        return {"update_id": counter["update"], **payload}

    def msg(text):
        return update(message={"from": {"id": OWNER}, "chat": {"id": OWNER}, "text": text})

    def cb(data):
        return update(callback_query={"id": "cb", "from": {"id": OWNER}, "message": {"chat": {"id": OWNER}},
                                      "data": json.dumps(data)})

    for step in SCENARIOS[scenario]["steps"]:
        kind = step[0]
        if kind == "say":
            events.append({"type": "user", "text": step[1]})
            await handler.handle_update(msg(step[1]))
        elif kind == "btn":
            events.append({"type": "user", "text": f"[кнопка {step[1]}]"})
            await handler.handle_update(cb({"a": step[1]}))
        elif kind == "log":
            with db_session() as db:
                s = open_session(db)
                plan = s.plans[-1] if s and s.plans else None
                item = next((i for i in (plan.items if plan else []) if step[1].lower() in i.exercise.name.lower()), None)
                item_id = item.id if item else None
            if item_id is None:
                events.append({"type": "error", "text": f"no plan item matching '{step[1]}' - plan missing or exercise dropped"})
                continue
            events.append({"type": "user", "text": f"[кнопка: {step[1]}] {step[2]}"})
            await handler.handle_update(cb({"a": "e", "i": item_id}))
            await handler.handle_update(msg(step[2]))
            with db_session() as db:
                s = open_session(db)
                pending = s.pending_entry if s else None
            if pending and pending.get("stage") == "comment":  # entry had no comment -> skip the prompt
                await handler.handle_update(cb({"a": "x"}))
        elif kind == "days":  # simulated time passing
            from datetime import timedelta
            import app.config
            app.config.clock_offset += timedelta(days=step[1])
            events.append({"type": "note", "text": f"⏩ прошло {step[1]} дней"})
        elif kind == "note":
            with db_session() as db:
                e = next((e for e in db.scalars(select(Exercise)).all() if step[1].lower() in e.name.lower()), None)
                events.append({"type": "note", "text": f"Заметка «{e.name}»: {e.rolling_note}" if e else "exercise missing"})

    with db_session() as db:
        checks = checks_for(scenario, db)
    out_path.write_text(json.dumps({"events": events, "checks": checks}, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- orchestrator

def run_model(model: str, scenarios: list[str], run_dir: Path) -> None:
    mdir = run_dir / model
    mdir.mkdir(parents=True, exist_ok=True)
    for sc in scenarios:
        db_file = mdir / f"{sc}.db"
        shutil.copy(SNAPSHOT, db_file)
        env = {**os.environ, "DATABASE_URL": f"sqlite:///{db_file.as_posix()}", "LLM_MODEL": model,
               "LLM_USAGE_FILE": str(mdir / f"{sc}.usage.jsonl"), "OWNER_TELEGRAM_ID": str(OWNER), "KEEP_TRANSCRIPTS": "0",
               "TELEGRAM_BOT_TOKEN": "eval", "PYTHONIOENCODING": "utf-8"}
        print(f"  [{model}] {sc} ...", flush=True)
        proc = subprocess.run([sys.executable, "-m", "scripts.eval_models", "--worker", sc,
                               "--out", str(mdir / f"{sc}.json")], cwd=ROOT, env=env,
                              capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            (mdir / f"{sc}.json").write_text(json.dumps({"events": [{"type": "error", "text": proc.stderr[-3000:]}],
                                                         "checks": []}, ensure_ascii=False), encoding="utf-8")


def usage_summary(mdir: Path) -> dict:
    cost, calls, seconds = 0.0, 0, []
    chat_seconds = []
    for f in mdir.glob("*.usage.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            calls += 1
            cost += row.get("cost_usd") or 0
            seconds.append(row["seconds"])
            if row["purpose"] == "chat":
                chat_seconds.append(row["seconds"])
    return {"cost": cost, "calls": calls,
            "avg_chat_s": sum(chat_seconds) / len(chat_seconds) if chat_seconds else 0}


def render(run_dir: Path, models: list[str], scenarios: list[str]) -> Path:
    def ev_html(ev):
        t = html.escape(ev.get("text", ""))
        if ev["type"] == "user":
            return f'<div class="u">🧑 {t}</div>'
        if ev["type"] == "bot":
            btn = "".join(f"<span class='b'>{html.escape(b)}</span>" for b in ev.get("buttons", []))
            return f'<div class="m">{t}{"<div>" + btn + "</div>" if btn else ""}</div>'
        if ev["type"] == "tool":
            return f'<div class="t">🔧 {html.escape(ev["name"])} {html.escape(json.dumps(ev["args"], ensure_ascii=False))[:400]}</div>'
        if ev["type"] == "note":
            return f'<div class="n">📒 {t}</div>'
        return f'<div class="e">❌ {t}</div>'

    out = ["<!doctype html><meta charset=utf-8><title>Model comparison</title><style>",
           "body{font:14px/1.45 system-ui;margin:16px;background:#f6f6f4;color:#222}",
           ".grid{display:grid;grid-template-columns:repeat(%d,1fr);gap:12px}" % len(models),
           ".col{background:#fff;border-radius:8px;padding:10px;overflow-wrap:anywhere}",
           ".u{background:#e3effd;padding:6px 8px;border-radius:6px;margin:6px 0}",
           ".m{white-space:pre-wrap;padding:6px 8px;border-left:3px solid #9b9;margin:6px 0}",
           ".t{font:12px monospace;color:#777;margin:2px 0}.n{background:#fff6d6;padding:6px;border-radius:6px}",
           ".e{color:#b00;white-space:pre-wrap;font:12px monospace}.b{display:inline-block;border:1px solid #ccc;border-radius:4px;padding:1px 6px;margin:2px;font-size:12px}",
           ".ok{color:#170}.bad{color:#b00;font-weight:600}h2{margin-top:32px}table{border-collapse:collapse}td,th{padding:4px 10px;border-bottom:1px solid #ddd}",
           "</style><h1>Model comparison</h1><table><tr><th>model</th><th>checks passed</th><th>est. cost</th><th>LLM calls</th><th>avg chat reply</th></tr>"]
    data = {m: {sc: json.loads((run_dir / m / f"{sc}.json").read_text(encoding="utf-8")) for sc in scenarios} for m in models}
    for m in models:
        u = usage_summary(run_dir / m)
        passed = sum(ok for sc in scenarios for _, ok in data[m][sc]["checks"])
        total = sum(len(data[m][sc]["checks"]) for sc in scenarios)
        out.append(f"<tr><td>{m}</td><td>{passed}/{total}</td><td>${u['cost']:.2f}</td><td>{u['calls']}</td><td>{u['avg_chat_s']:.1f}s</td></tr>")
    out.append("</table>")
    for sc in scenarios:
        out.append(f"<h2>{sc}</h2><p>{html.escape(SCENARIOS[sc]['about'])}</p><div class=grid>")
        for m in models:
            d = data[m][sc]
            checks = "".join(f"<div class={'ok' if ok else 'bad'}>{'✔' if ok else '✘'} {html.escape(n)}</div>" for n, ok in d["checks"])
            out.append(f"<div class=col><b>{m}</b>{checks}{''.join(ev_html(e) for e in d['events'])}</div>")
        out.append("</div>")
    path = run_dir / "report.html"
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["claude-sonnet-5", "gemini-3.8-flash"])
    parser.add_argument("--only", nargs="+", default=list(SCENARIOS))
    parser.add_argument("--worker")
    parser.add_argument("--out")
    args = parser.parse_args()
    if args.worker:
        asyncio.run(run_worker(args.worker, Path(args.out)))
        return
    if not SNAPSHOT.exists():
        raise SystemExit(f"Snapshot {SNAPSHOT} missing - run the import first (it creates the snapshot).")
    run_dir = ROOT / "eval_runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"Running {len(args.only)} scenarios x {len(args.models)} models -> {run_dir}")
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(len(args.models)) as pool:
        list(pool.map(lambda m: run_model(m, args.only, run_dir), args.models))
    report = render(run_dir, args.models, args.only)
    print(f"Report: {report}")
    webbrowser.open(report.as_uri())


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
