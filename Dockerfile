# Cloud Run image: `gcloud run deploy --source .` builds this file (see scripts/deploy.py).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
# bytecode baked into the image: less work per cold start
RUN python -m compileall -q app

# Cloud Run sets $PORT (8080 by default)
CMD exec uvicorn app.webhook:app --host 0.0.0.0 --port ${PORT:-8080} --no-access-log
