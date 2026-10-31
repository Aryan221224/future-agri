FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir "asyncpg>=0.29" "aiomqtt>=2.0"
COPY services/ingest/fa_ingest /app/fa_ingest
CMD ["python", "-m", "fa_ingest.main"]
