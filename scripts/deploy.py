"""Deploys the bot to Google Cloud Run and wires up Telegram + the weekly scheduler. Safe to re-run
after every code change (it updates in place).

    .venv\\Scripts\\python -m scripts.deploy --project <gcp-project-id>

Needs the gcloud CLI, logged in (`gcloud auth login`). Reads .env:
  TELEGRAM_BOT_TOKEN, OWNER_TELEGRAM_ID, GEMINI_API_KEY / ANTHROPIC_API_KEY, LLM_* , HANDLER_MODE, TIMEZONE
  CLOUD_DATABASE_URL  - Supabase connection string (becomes DATABASE_URL on the cloud; local .env keeps SQLite)
  WEBHOOK_SECRET, JOBS_SECRET - generated into .env on the first run if empty
"""

import argparse
import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
SERVICE = "coach-bot"
# Cloud Scheduler jobs: name -> (cron in TIMEZONE, endpoint). The free tier covers 3 jobs.
JOBS = {
    "coach-weekly": ("0 10 * * 0", "/jobs/weekly"),       # Sundays 10:00: weigh-in reminder, backup, token cost
    "coach-keepalive": ("0 6 * * *", "/jobs/keepalive"),  # daily: keeps the free Supabase project from pausing
}
APIS = ["run.googleapis.com", "cloudbuild.googleapis.com", "artifactregistry.googleapis.com",
        "cloudscheduler.googleapis.com"]
PASSED_THROUGH = ["TELEGRAM_BOT_TOKEN", "OWNER_TELEGRAM_ID", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "LLM_MODEL",
                  "LLM_EFFORT", "LLM_SYNTHESIS_EFFORT", "HANDLER_MODE", "TIMEZONE", "WEBHOOK_SECRET", "JOBS_SECRET"]

GCLOUD = (shutil.which("gcloud") or shutil.which("gcloud.cmd") or  # winget installs without updating this shell's PATH
          shutil.which(Path(os.getenv("LOCALAPPDATA", "")) / "Google/Cloud SDK/google-cloud-sdk/bin/gcloud.cmd"))


def gcloud(*args: str, capture: bool = False) -> str:
    print("  > gcloud " + " ".join(a if "secret=" not in a.lower() else a.rsplit("=", 1)[0] + "=***" for a in args))
    run = subprocess.run([GCLOUD, *args, "--quiet"], text=True, capture_output=capture)
    if run.returncode != 0:
        sys.exit(f"gcloud failed{': ' + run.stderr.strip() if capture else ''}")
    return (run.stdout or "").strip()


def ensure_secrets() -> dict:
    env = dotenv_values(ENV_FILE)
    added = []
    for key in ("WEBHOOK_SECRET", "JOBS_SECRET"):
        if not env.get(key):
            value = secrets.token_urlsafe(32)  # Telegram allows A-Z a-z 0-9 _ -
            text = ENV_FILE.read_text(encoding="utf-8")
            if f"\n{key}=" in f"\n{text}":
                text = "\n".join(f"{key}={value}" if line.startswith(f"{key}=") else line for line in text.splitlines()) + "\n"
            else:
                text = text.rstrip("\n") + f"\n{key}={value}\n"
            ENV_FILE.write_text(text, encoding="utf-8")
            added.append(key)
    if added:
        print(f"Generated {', '.join(added)} into .env")
    return dotenv_values(ENV_FILE)


def env_vars_file(env: dict) -> str:
    if not env.get("CLOUD_DATABASE_URL"):
        sys.exit("Put the Supabase connection string into .env as CLOUD_DATABASE_URL=... first")
    values = {k: env[k] for k in PASSED_THROUGH if env.get(k)}
    values.update(DATABASE_URL=env["CLOUD_DATABASE_URL"], TRANSCRIPT_DIR="", LLM_USAGE_FILE="")
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for k, v in values.items():
            f.write(f"{k}: '{v.replace(chr(39), chr(39) * 2)}'\n")  # YAML single-quoted string
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="Google Cloud project ID")
    ap.add_argument("--region", default="europe-west1")
    args = ap.parse_args()
    if not GCLOUD:
        sys.exit("gcloud CLI not found - install the Google Cloud SDK first")
    env = ensure_secrets()
    common = ["--project", args.project]

    print("1/4 Enabling Google Cloud services (first run takes a minute)")
    gcloud("services", "enable", *APIS, *common)

    print("2/4 Building and deploying the bot (3-5 minutes)")
    vars_path = env_vars_file(env)
    try:
        gcloud("run", "deploy", SERVICE, "--source", str(ROOT), "--region", args.region, *common,
               "--allow-unauthenticated",     # Telegram must reach it; the secret headers guard the endpoints
               "--max-instances", "1",        # one process -> the handler lock serializes all updates
               "--min-instances", "0",        # scale to zero when idle (free)
               "--cpu", "1", "--memory", "512Mi", "--timeout", "600",
               "--env-vars-file", vars_path)
    finally:
        os.remove(vars_path)
    url = gcloud("run", "services", "describe", SERVICE, "--region", args.region, *common,
                 "--format", "value(status.url)", capture=True)
    print(f"   Service URL: {url}")

    print("3/4 Pointing Telegram at the cloud bot")
    os.environ["WEBHOOK_SECRET"] = env["WEBHOOK_SECRET"]
    from app import telegram  # after .env is complete
    asyncio.run(telegram.set_webhook(url + "/telegram", env["WEBHOOK_SECRET"]))
    print(f"   Webhook set: {url}/telegram")

    print("4/4 Scheduled jobs (weekly reminder + backup, daily database keep-alive)")
    for name, (schedule, path) in JOBS.items():
        exists = subprocess.run([GCLOUD, "scheduler", "jobs", "describe", name, "--location", args.region, *common],
                                capture_output=True, text=True).returncode == 0
        job = ["--location", args.region, *common, "--schedule", schedule,
               "--time-zone", env.get("TIMEZONE") or "Europe/Warsaw", "--uri", f"{url}{path}",
               "--http-method", "POST", "--attempt-deadline", "300s", "--format", "none"]
        if exists:
            gcloud("scheduler", "jobs", "update", "http", name, *job, f"--update-headers=X-Jobs-Secret={env['JOBS_SECRET']}")
        else:
            gcloud("scheduler", "jobs", "create", "http", name, *job, f"--headers=X-Jobs-Secret={env['JOBS_SECRET']}")
    print(f"\nDone. Test the weekly job now with:\n"
          f"  gcloud scheduler jobs run coach-weekly --location {args.region} --project {args.project}")


if __name__ == "__main__":
    main()
