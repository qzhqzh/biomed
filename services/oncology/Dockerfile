FROM python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ONCO_DATA_DIR=/app/var
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src ./src
COPY data/demo ./data/demo
RUN mkdir -p /app/var && chown -R 1000:1000 /app
USER 1000:1000
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "oncology_workbench.main:app", "--app-dir", "src", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
