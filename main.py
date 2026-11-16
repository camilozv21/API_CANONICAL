"""API de solo lectura para un territorial_canonical_bundle (POC)."""
from __future__ import annotations

import gzip
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse

DATA_FILE = Path(os.getenv("TERRITORIAL_JSON", "data/territorial_canonical_bundle.json")).expanduser()
# Cargar el bundle al arrancar (y no en la primera petición): /health solo responde cuando ya hay datos.
PRELOAD = os.getenv("TERRITORIAL_PRELOAD", "1").strip().lower() not in ("0", "false", "no")
log = logging.getLogger("uvicorn.error")
_lock = RLock()
_bundle: dict[str, Any] = {}
_records: list[dict[str, Any]] = []
_datasets: list[dict[str, Any]] = []
_by_variable: dict[str, list[dict[str, Any]]] = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    if PRELOAD:
        try:
            _ensure_loaded()
            log.info("Bundle cargado: %d registros indexados en %d datasets.", len(_records), len(_datasets))
        except HTTPException as exc:
            log.error("No se pudo cargar el bundle al iniciar: %s", exc.detail)
    yield


app = FastAPI(title="Territorial Intelligence API", version="0.2.0", description="API de consulta para JSON canónico unificado, sin modificar los registros originales.",
              lifespan=lifespan)


def _open_data(path: Path):
    """Abre el bundle como texto: JSON plano o comprimido con gzip (.json.gz, ~19 MB en vez de ~820 MB)."""
    with path.open("rb") as fh:
        compressed = fh.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rt", encoding="utf-8-sig") if compressed else path.open("r", encoding="utf-8-sig")


def _read_file() -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    if not DATA_FILE.is_file():
        raise ValueError(f"No existe el archivo: {DATA_FILE.resolve()}")
    try:
        with _open_data(DATA_FILE) as fp:
            bundle = json.load(fp)
    except (json.JSONDecodeError, UnicodeError, OSError, EOFError) as exc:
        raise ValueError(f"El archivo no es un JSON válido: {exc}") from exc
    if not isinstance(bundle, dict) or bundle.get("schema") != "territorial_canonical_bundle":
        raise ValueError("Se espera un JSON con schema='territorial_canonical_bundle'.")
    variables = bundle.get("variables")
    if not isinstance(variables, dict):
        raise ValueError("El campo 'variables' debe ser un objeto JSON.")

    records: list[dict[str, Any]] = []
    datasets: list[dict[str, Any]] = []
    by_variable: dict[str, list[dict[str, Any]]] = {}
    for variable_group, blocks in variables.items():
        bucket = by_variable.setdefault(variable_group, [])
        if not isinstance(blocks, list):
            continue
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            dataset = {k: v for k, v in block.items() if k != "data"}
            dataset.update({"variable_group": variable_group, "dataset_index": i})
            datasets.append(dataset)
            # El bundle agrupa datos por variable y archivo de origen.
            # El modelo canónico puede preservar contenido de varias formas.
            # Para endpoints de observaciones se detectan registros con grid_id.
            def walk(node: Any) -> None:
                if isinstance(node, list):
                    for element in node:
                        walk(element)
                elif isinstance(node, dict):
                    if "grid_id" in node or "spatial_id" in node:
                        entry = {"variable_group": variable_group,
                                 "dataset_index": i,
                                 "source_file": block.get("source_file"),
                                 "record": node}
                        records.append(entry)
                        bucket.append(entry)
                    else:
                        for value in node.values():
                            if isinstance(value, (list, dict)):
                                walk(value)
            walk(block.get("data"))
    return bundle, records, datasets, by_variable


def _ensure_loaded() -> None:
    global _bundle, _records, _datasets, _by_variable
    with _lock:
        if _bundle:
            return
        try:
            _bundle, _records, _datasets, _by_variable = _read_file()
        except ValueError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc


def _iter_json(node: Any) -> Iterator[str]:
    """Mismo texto que json.dumps compacto, pero por partes: los contenedores grandes se recorren
    y cada registro se serializa por separado, sin construir en memoria el JSON entero."""
    if isinstance(node, dict) and any(isinstance(v, (dict, list)) for v in node.values()):
        yield "{"
        for n, (key, value) in enumerate(node.items()):
            yield ("," if n else "") + json.dumps(str(key), ensure_ascii=False) + ":"
            yield from _iter_json(value)
        yield "}"
    elif isinstance(node, list) and len(node) > 64:
        yield "["
        for n, value in enumerate(node):
            if n:
                yield ","
            yield from _iter_json(value)
        yield "]"
    else:
        yield json.dumps(node, ensure_ascii=False, separators=(",", ":"))


def _stream_json(node: Any) -> StreamingResponse:
    def chunks() -> Iterator[bytes]:
        buf: list[str] = []
        size = 0
        for part in _iter_json(node):
            buf.append(part)
            size += len(part)
            if size >= 1 << 16:
                yield "".join(buf).encode("utf-8")
                buf, size = [], 0
        if buf:
            yield "".join(buf).encode("utf-8")
    return StreamingResponse(chunks(), media_type="application/json")


def _date_of(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _match_date(record: dict[str, Any], start: date | None, end: date | None) -> bool:
    """Filtra por solapamiento del intervalo [time_start, time_end)."""
    if start is None and end is None:
        return True
    a = _date_of(record.get("time_start"))
    b = _date_of(record.get("time_end"))
    if a is None and b is None:
        return False
    if a is None:
        a = b
    if b is None:
        b = a
    # fecha_hasta se interpreta como día inclusivo; time_end es exclusivo.
    if start is not None and b is not None and b <= start:
        return False
    if end is not None and a is not None and a > end:
        return False
    return True


@app.get("/health", tags=["Sistema"])
def health() -> dict[str, Any]:
    _ensure_loaded()
    return {"status": "ok", "schema": _bundle.get("schema"),
            "schema_version": _bundle.get("schema_version"),
            "records_indexed": len(_records), "datasets": len(_datasets)}


@app.get("/v1/summary", tags=["Catálogo"])
def summary() -> dict[str, Any]:
    _ensure_loaded()
    return {"schema": _bundle.get("schema"), "schema_version": _bundle.get("schema_version"),
            "generated_at_utc": _bundle.get("generated_at_utc"),
            "summary": _bundle.get("summary"), "manifest": _bundle.get("manifest"),
            "indexed_records": len(_records)}


@app.get("/v1/variables", tags=["Catálogo"])
def variables() -> dict[str, Any]:
    _ensure_loaded()
    out = []
    for name, blocks in _bundle["variables"].items():
        out.append({"name": name, "datasets": len(blocks) if isinstance(blocks, list) else 0,
                    "indexed_records": len(_by_variable.get(name, []))})
    return {"items": out}


@app.get("/v1/datasets", tags=["Catálogo"])
def datasets(variable: str | None = None) -> dict[str, Any]:
    _ensure_loaded()
    items = [d for d in _datasets if variable is None or d["variable_group"] == variable]
    return {"total": len(items), "items": items}


@app.get("/v1/observations", tags=["Datos"])
def observations(
    variable: str | None = Query(None, description="Grupo canónico, ej. ndvi"),
    grid_id: str | None = None,
    source: str | None = None,
    quality_flag: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    min_lat: float | None = Query(None, ge=-90, le=90),
    max_lat: float | None = Query(None, ge=-90, le=90),
    min_lon: float | None = Query(None, ge=-180, le=180),
    max_lon: float | None = Query(None, ge=-180, le=180),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    _ensure_loaded()
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from debe ser <= date_to")
    if min_lat is not None and max_lat is not None and min_lat > max_lat:
        raise HTTPException(status_code=422, detail="min_lat debe ser <= max_lat")
    if min_lon is not None and max_lon is not None and min_lon > max_lon:
        raise HTTPException(status_code=422, detail="min_lon debe ser <= max_lon")

    items = []
    for entry in (_records if variable is None else _by_variable.get(variable, [])):
        record = entry["record"]
        if grid_id is not None and str(record.get("grid_id", record.get("spatial_id"))) != grid_id:
            continue
        if source is not None and record.get("source") != source:
            continue
        if quality_flag is not None and record.get("quality_flag") != quality_flag:
            continue
        if not _match_date(record, date_from, date_to):
            continue
        lat, lon = record.get("latitude"), record.get("longitude")
        if any(x is not None for x in (min_lat, max_lat)):
            if not isinstance(lat, (int, float)) or (min_lat is not None and lat < min_lat) or (max_lat is not None and lat > max_lat):
                continue
        if any(x is not None for x in (min_lon, max_lon)):
            if not isinstance(lon, (int, float)) or (min_lon is not None and lon < min_lon) or (max_lon is not None and lon > max_lon):
                continue
        items.append(entry)
    return {"total": len(items), "limit": limit, "offset": offset, "items": items[offset:offset + limit]}


@app.get("/v1/grids/{grid_id}", tags=["Datos"])
def grid(grid_id: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    _ensure_loaded()
    items = [entry for entry in _records if str(entry["record"].get("grid_id", entry["record"].get("spatial_id"))) == grid_id]
    return {"grid_id": grid_id, "total": len(items), "limit": limit, "offset": offset,
            "items": items[offset:offset + limit]}


@app.get("/v1/export/{variable}", tags=["Datos"])
def export_variable(
    variable: str,
    fields: str | None = Query(None, description="Campos separados por coma, ej. grid_id,value. Por defecto, el registro íntegro."),
) -> StreamingResponse:
    """Todos los registros indexados de una variable en NDJSON (un registro JSON por línea), en streaming.

    Para consumidores que necesitan la variable completa (p. ej. el motor agronómico) sin paginar.
    Con `fields` solo viajan esos campos; los que un registro no tiene se omiten, no se rellenan.
    La cabecera `X-Record-Count` trae el número de líneas para verificar que llegó todo.
    """
    _ensure_loaded()
    entries = _by_variable.get(variable)
    if entries is None:
        raise HTTPException(status_code=404, detail=f"Variable no encontrada: {variable}")
    wanted = [f.strip() for f in fields.split(",") if f.strip()] if fields else None

    def lines() -> Iterator[bytes]:
        buf: list[str] = []
        for entry in entries:
            record = entry["record"]
            if wanted is not None:
                record = {k: record[k] for k in wanted if k in record}
            buf.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            if len(buf) >= 2000:
                yield ("\n".join(buf) + "\n").encode("utf-8")
                buf.clear()
        if buf:
            yield ("\n".join(buf) + "\n").encode("utf-8")

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"X-Record-Count": str(len(entries))})


@app.get("/v1/raw/{variable}/{dataset_index}", tags=["Datos"])
def raw_dataset(variable: str, dataset_index: int) -> StreamingResponse:
    """Devuelve exactamente el objeto del dataset original (sin transformar), en streaming."""
    _ensure_loaded()
    blocks = _bundle["variables"].get(variable)
    if not isinstance(blocks, list) or not (0 <= dataset_index < len(blocks)):
        raise HTTPException(status_code=404, detail="Dataset no encontrado")
    return _stream_json(blocks[dataset_index])


@app.get("/v1/raw-bundle", tags=["Datos"])
def raw_bundle() -> StreamingResponse:
    """Devuelve el JSON canónico íntegro, en streaming (cientos de MB: no abrir desde /docs)."""
    _ensure_loaded()
    return _stream_json(_bundle)


@app.post("/v1/reload", tags=["Sistema"])
def reload_json() -> dict[str, Any]:
    """Recarga el archivo. Restringir con autenticación antes de exponer en internet."""
    global _bundle, _records, _datasets, _by_variable
    try:
        next_bundle, next_records, next_datasets, next_by_variable = _read_file()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    with _lock:
        _bundle, _records, _datasets, _by_variable = next_bundle, next_records, next_datasets, next_by_variable
    return {"status": "reloaded", "records_indexed": len(_records),
            "datasets": len(_datasets)}
