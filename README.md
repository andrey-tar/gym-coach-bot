# Personal Gym Coach (Telegram bot)

A single-user Telegram bot that coaches strength + cardio training: it proposes the day's workout, argues
about it, logs results via inline buttons and keeps long-term memory in a database instead of an ever-growing
chat. The model is stateless - every call gets a bounded context assembled from structured data (rolling notes per
exercise/split, injuries, body metrics), so cost and quality don't degrade over months of use.

Full design: `gym_coach_bot_architecture.md`. Python 3.13, Gemini / Claude (switchable live with `/model`),
SQLAlchemy (SQLite locally, Supabase Postgres in prod), Google Cloud Run + Cloud Scheduler.

## Layout
- `app/config.py` - all settings (.env); stale-session thresholds and other Section 13 tunables
- `app/models.py` - data model (spec Section 6)
- `app/context.py` - bounded context assembly per call (spec 2, 3.1, 4)
- `app/tools.py` - model tools: history/profile reads, all state writes + confirmation footnotes
- `app/handler.py` - update flow: owner check, update_id dedupe, lazy close, logging buttons, /final, combined/separate modes
- `app/synthesis.py` - end-of-session synthesis pipeline (spec 5)
- `app/jobs.py` - weekly weigh-in reminder + JSON backup to Telegram (spec 8, 9)
- `app/run_polling.py` - local runner (long polling); refuses to start while the cloud webhook is live
- `app/webhook.py` - production server (Cloud Run): `/telegram` webhook, `/jobs/weekly` for Cloud Scheduler
- `scripts/import_history.py` - one-time spreadsheet import (spec Appendix); blank sheet: `training_history_import_template.xlsx`
- `scripts/eval_models.py` - Section 7 scripted model comparison (HTML report)
- `scripts/check_setup.py` - verifies the keys in .env
- `scripts/migrate_to_postgres.py` - one-time copy of data/coach.db into Supabase
- `scripts/deploy.py` - deploy to Cloud Run + set the Telegram webhook + the Sunday 10:00 scheduler job
- `scripts/set_webhook.py` - show / set / remove the Telegram webhook

## Local run
```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env   # fill in TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID, ANTHROPIC_API_KEY
.venv\Scripts\python -m scripts.import_history --dry-run
.venv\Scripts\python -m scripts.import_history
.venv\Scripts\python -m app.run_polling
```

## Not built yet
- Section 7 A/B harness (combined vs separate handler) - both modes exist (`HANDLER_MODE`), the scripted comparison runner is next

## Cloud (Google Cloud Run + Supabase)
```
.venv\Scripts\python -m scripts.migrate_to_postgres "<supabase url>" --apply   # once
.venv\Scripts\python -m scripts.deploy --project <gcp-project-id>               # after every code change
```
Logs: Cloud Run > coach-bot > Logs. Cost log and transcripts: `llm_usage` / `archived_transcripts` tables.
