FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.30" \
    "asyncpg>=0.29" "pydantic>=2.7"
COPY services/api/fa_api /app/fa_api
EXPOSE 8000
CMD ["uvicorn", "fa_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
