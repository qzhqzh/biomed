FROM node:22-bookworm AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build:docker

FROM ensemblorg/ensembl-vep:release_116.0

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        python3-venv \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock /app/requirements.lock
RUN python3 -m venv /opt/biotools && \
    /opt/biotools/bin/pip install --no-cache-dir -r /app/requirements.lock

COPY manage.py /app/manage.py
COPY config /app/config
COPY apps /app/apps
COPY scripts/import_gene_knowledge.py /app/scripts/import_gene_knowledge.py
COPY reference-manifest.yaml /app/reference-manifest.yaml
COPY --from=frontend /frontend/dist /app/apps/portal/static/portal/dist

RUN DJANGO_DEBUG=1 /opt/biotools/bin/python manage.py collectstatic --noinput

ENV PATH="/opt/biotools/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VEP_BIN=/opt/vep/src/ensembl-vep/vep \
    VEP_CACHE_DIR=/data/vep \
    VEP_ASSEMBLY=GRCh37 \
    DJANGO_DEBUG=0 \
    DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

RUN mkdir -p /data/vep /data/tmp /app/var && \
    useradd --create-home --uid 10001 biotools && \
    chown -R biotools:biotools /app /data

USER biotools

EXPOSE 8000

CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 320"]
