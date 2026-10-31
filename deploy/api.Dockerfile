FROM python:3.11-slim
WORKDIR /app
# The advisory endpoints run the crop twin, so the API image carries the full
# runtime (PCSE, OR-Tools, numpy/pandas) as well as the web stack.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY packages /app/packages
COPY services /app/services
COPY vendor /app/vendor
ENV PYTHONPATH=/app/packages/fa_core:/app/services/twin:/app/services/planner:/app/services/gateway:/app/services/loop:/app/services/api
EXPOSE 8000
CMD ["uvicorn", "fa_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
