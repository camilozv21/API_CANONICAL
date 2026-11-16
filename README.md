# Territorial Intelligence API — POC

API REST de solo lectura basada en el bundle `territorial_canonical_bundle`.

## Iniciar (Windows / PowerShell)

```powershell
cd territorial_api
py -m pip install -r requirements.txt
$env:TERRITORIAL_JSON = "C:\ruta\a\tu\territorial_canonical_bundle.json"
py -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Si guardas el archivo en `territorial_api/data/territorial_canonical_bundle.json`, no necesitas la variable de entorno. Crea la carpeta `data` manualmente y coloca allí el JSON **completo**, no el texto pegado truncado.

Abrir documentación interactiva en `http://127.0.0.1:8000/docs`.

## Docker

```bash
docker compose up --build -d     # http://127.0.0.1:8001/docs
```

- El bundle va **dentro de la imagen**, comprimido: `territorial_canonical_unified.json.gz` (~16 MB en vez de ~820 MB).
  La API lee `.json` o `.json.gz` indistintamente (`TERRITORIAL_JSON`). Al cambiar el bundle, regenerar el `.gz` y reconstruir:
  `gzip -9 -n -c territorial_canonical_unified.json > territorial_canonical_unified.json.gz`
- Imagen publicada: `milozap21/api-canonica:latest` (= `0.2.0`, con datos, linux/amd64). `0.1.0` es la versión sin datos.
- El bundle se carga al arrancar (~20-30 s, ~1.6 GB de RAM; pico ~3 GB durante la carga). El healthcheck espera a que esté listo.
  Docker Desktop necesita al menos 4 GB de memoria asignada. Otro puerto local: `CANONICA_PORT`.
- El puerto se publica solo en `127.0.0.1` (la API no tiene autenticación).
- El motor agronómico (`backend_hackathon/Motor_agronomico`) incluye este `docker-compose.yml` en el suyo y consume la API por la red interna (`http://canonica:8000`).

## Endpoints

- `GET /health`: verifica carga del archivo.
- `GET /v1/summary`: metadatos de consolidación.
- `GET /v1/variables`: variables y conteos.
- `GET /v1/datasets?variable=ndvi`: archivos origen por variable.
- `GET /v1/observations?variable=ndvi&grid_id=0&limit=10`: observaciones por filtros.
- `GET /v1/observations?date_from=2026-05-01&date_to=2026-05-31&quality_flag=valid`: filtro temporal/calidad.
- `GET /v1/observations?min_lat=6&max_lat=7&min_lon=-76&max_lon=-75`: bounding box.
- `GET /v1/grids/0`: todas las observaciones de una celda.
- `GET /v1/export/soil?fields=grid_id,variable,value`: todos los registros de una variable en NDJSON (uno por línea), en streaming. Sin `fields`, el registro íntegro. La cabecera `X-Record-Count` trae el total. Es lo que usa el motor agronómico.
- `GET /v1/raw/ndvi/0`: dataset original completo (en streaming).
- `GET /v1/raw-bundle`: todo el JSON original (en streaming, ~500 MB: no abrir desde `/docs`).
- `POST /v1/reload`: recarga JSON tras reemplazar el archivo (sin reiniciar proceso).

Todas las observaciones retornan `total`, `limit`, `offset` e `items`. Cada item contiene `record` íntegro, `source_file`, `variable_group` y `dataset_index`.

**Aclaraciones:**
- El endpoint `/v1/observations` indexa objetos que contienen `grid_id` o `spatial_id`. Si un modelo canónico tiene otra estructura sin estos campos, permanece accesible mediante `/v1/raw/...` pero no aparecerá en las observaciones indexadas.
- El intervalo de observación se interpreta como `[time_start, time_end)`; `date_from` y `date_to` se utilizan para encontrar solapamientos (con `date_to` inclusivo a nivel día).
- No se elimina ni modifica ningún registro original, incluidos `value: null` y campos de calidad.
- No expongas esta POC directamente a internet: no tiene autenticación ni control de acceso. Usa un proxy seguro y agrega autenticación antes del despliegue público.
- En esta versión el archivo se carga en memoria. Para millones de registros, se recomienda migrar a PostgreSQL/PostGIS o DuckDB.
