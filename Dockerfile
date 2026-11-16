# Territorial Intelligence API (FastAPI) con el bundle canónico incluido, comprimido (~16 MB en vez de ~820 MB).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000 \
    TERRITORIAL_JSON=/app/data/territorial_canonical_unified.json.gz

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# Datos antes que el código: cambian menos y así su capa queda en caché.
# Regenerar al cambiar el bundle:  gzip -9 -n -c territorial_canonical_unified.json > territorial_canonical_unified.json.gz
COPY territorial_canonical_unified.json.gz data/
COPY main.py .

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

# El bundle se carga al arrancar (~20-30 s, ~1.6 GB de RAM, pico ~3 GB): uvicorn no acepta conexiones
# hasta tenerlo, y /health responde 503 si el archivo falta o no es válido.
HEALTHCHECK --interval=10s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4)"

# Un solo proceso: cada worker cargaría su propia copia del bundle en memoria.
CMD exec uvicorn main:app --host 0.0.0.0 --port "${PORT}" --proxy-headers --forwarded-allow-ips="*"
