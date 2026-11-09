# Vekro backend image (M26, #285). One image runs the API, the scheduler and migrations;
# deploy/compose.yaml picks the command. Versions are pinned by deploy/constraints.txt, the set
# the test suite passed with.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt deploy/constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY api ./api
# Operator scripts that run against the live database (docs/hosting.md, docs/anchoring.md).
COPY scripts/audit_anchoring.py scripts/verify_transaction.py ./scripts/

RUN useradd --system --uid 10001 --home-dir /app vekro && chown -R vekro /app
USER vekro

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

# Behind the proxy (Caddy or the web image's nginx) on the private Compose network only.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
