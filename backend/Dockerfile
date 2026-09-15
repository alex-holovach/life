FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58
WORKDIR /app
COPY --chmod=0644 pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY --chmod=0644 app.py storage.py remote_write.py worker.py whoop_protocol.py temperature.py temperature_reference.py temperature_reprocess.py hrv.py hrv_analysis.py analysis_quality.py respiration.py vitals.py wellness.py ./
COPY integrations ./integrations
RUN chmod -R a+rX integrations
ENV PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
