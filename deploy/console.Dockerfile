FROM python:3.12-slim
WORKDIR /app
# The Mission Console: the simulation stack (PCSE, OR-Tools, the forecast archives'
# xarray/zarr/icechunk, rasterio) plus FastAPI. The SPA is pre-built into
# services/console/fa_console/static, so no Node in the image.
COPY scripts/console/requirements-worker.txt /app/scripts/console/requirements-worker.txt
COPY services/console/requirements.txt /app/services/console/requirements.txt
RUN pip install --no-cache-dir -r /app/services/console/requirements.txt
COPY packages /app/packages
COPY services /app/services
COPY vendor /app/vendor
# No login: the container listens on all interfaces, and compose publishes it on the
# host's loopback only. Do not publish it on a network you do not trust.
ENV PYTHONPATH=/app/services/console FA_HOME=/data/console FA_DATA_CACHE=/data/cache FA_BIND=0.0.0.0
VOLUME /data
EXPOSE 8099
CMD ["python", "-m", "fa_console"]
