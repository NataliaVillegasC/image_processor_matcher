"""Etapa 5 - selection with Gemini (Vertex AI).

One multimodal call per product: the <=6 candidates that survived the
pre-filter (Etapa 4), numbered, plus a prompt asking for structured JSON
(`response_mime_type="application/json"` + `response_schema`). Gemini scores
each image against the eliminatory criteria (correct product, brand) and the
quality criteria (resolution, background, condition, packaging, cleanliness),
and PICKS the best one -> the code doesn't re-choose from scratch, it only
acts on that pick for the edge cases (see `_postprocess`).

Default model: Flash (cheap, the task is discrimination not generation).
`MODEL_PRO` is declared for the two-tier routing (low
confidence -> Pro) but that auto-routing is NOT implemented here - call with
`model=MODEL_PRO` explicitly when that's decided.

Images ARE downloaded in full here because Gemini needs the real bytes; they're re-encoded to JPEG
and capped at MAX_SIDE px to keep the per-product cost down.
"""
import json
import os
import re
import time
from io import BytesIO
from pathlib import Path

import requests
from google import genai
from google.genai import errors, types
from PIL import Image

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "select"

MODEL_FLASH = "gemini-2.5-flash"
MODEL_PRO = "gemini-2.5-pro"

MAX_SIDE = 1024      # resize before sending -> controls tokens/cost per image
JPEG_QUALITY = 85


# --- Vertex AI client (lazy singleton) --------------------------------------

_CLIENT: genai.Client | None = None


def _client() -> genai.Client:
    global _CLIENT
    if _CLIENT is None:
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "")
        location = os.getenv("GOOGLE_CLOUD_LOCATION", "")
        if not project or not location:
            raise RuntimeError(
                "GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION no configuradas en .env"
            )
        _CLIENT = genai.Client(vertexai=True, project=project, location=location)
    return _CLIENT


# --- Prompt (template, plan.md Etapa 5) -------------------------------------
# The fields interpolated in are the catalog's own Spanish data (product name,
# category, presentation rule) - that's expected and fine to mix.

PROMPT_TEMPLATE = """You are an image verifier for an auto-parts catalog.

PRODUCT: {nombre_limpio}
REQUIRED BRAND: {marca}
CATEGORY: {categoria}
PRESENTATION RULE: {presentacion}

You receive {n} candidate images numbered 1 to {n}. Evaluate each one against \
the criteria below, then PICK the best one among those that pass the eliminatory criteria:

ELIMINATORY CRITERIA (fail one and the image is discarded):
1. PRODUCT: does the image show exactly this type of part? Check the quantity
    and presentation against the rule given. Count the visible units.
2. BRAND: is there visible evidence of the required brand (logo on the part,
    engraving, legible packaging)? If there's no visible evidence but also no
    evidence of a different brand, mark it "no_verificable" instead of discarding it.

QUALITY CRITERIA (score 0-10 each):
- resolucion: apparent resolution and sharpness.
- fondo: white background (10) / monochrome (6) / cluttered (0).
- estado: product looks new (no wear, rust, or use).
- empaque: score high when there's NO box/bag/packaging - EXCEPTION: if the
    packaging is the only visible evidence of the brand, it adds instead of subtracting.
- limpieza: no watermarks, overlaid text, store logos, or people.

Pick the best image, prioritizing passing the eliminatory criteria first, then
the quality total. If no candidate passes the eliminatory criteria, seleccion=null
and ranking=[]."""


def build_prompt(nombre_limpio: str, marca: str, categoria: str, presentacion: str,
                n: int) -> str:
    return PROMPT_TEMPLATE.format(
        nombre_limpio=nombre_limpio, marca=marca, categoria=categoria,
        presentacion=presentacion, n=n,
    )


# --- Response schema (same field names as the JSON in the plan) ------------

SELECT_SCHEMA = types.Schema(
    type="OBJECT",
    properties={
        "evaluaciones": types.Schema(
            type="ARRAY",
            items=types.Schema(
                type="OBJECT",
                properties={
                    "imagen": types.Schema(type="INTEGER"),
                    "producto_correcto": types.Schema(type="BOOLEAN"),
                    "unidades_visibles": types.Schema(type="INTEGER"),
                    "marca": types.Schema(
                        type="STRING",
                        enum=["verificada", "no_verificable", "incorrecta"],
                    ),
                    "evidencia_marca": types.Schema(type="STRING"),
                    "scores": types.Schema(
                        type="OBJECT",
                        properties={
                            "resolucion": types.Schema(type="INTEGER"),
                            "fondo": types.Schema(type="INTEGER"),
                            "estado": types.Schema(type="INTEGER"),
                            "empaque": types.Schema(type="INTEGER"),
                            "limpieza": types.Schema(type="INTEGER"),
                        },
                        required=["resolucion", "fondo", "estado", "empaque", "limpieza"],
                    ),
                    "descartada": types.Schema(type="BOOLEAN"),
                    "razon": types.Schema(type="STRING"),
                },
                required=["imagen", "producto_correcto", "unidades_visibles", "marca",
                        "evidencia_marca", "scores", "descartada", "razon"],
            ),
        ),
        "seleccion": types.Schema(type="INTEGER", nullable=True),
        "ranking": types.Schema(type="ARRAY", items=types.Schema(type="INTEGER")),
        "confianza": types.Schema(type="STRING", enum=["alta", "media", "baja"]),
        "comentario": types.Schema(type="STRING"),
    },
    required=["evaluaciones", "seleccion", "ranking", "confianza", "comentario"],
)


# --- Image fetch (full-size, in memory only) --------------------------------

# Headers of a real browser: many stores answer 403 to anything that looks
# like a bot, even though the same image loads fine in Chrome.
FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
    "Referer": "https://www.google.com/",
}


def fetch_full_image(url: str, timeout: int = 15, max_side: int = MAX_SIDE,
                    quality: int = JPEG_QUALITY) -> bytes | None:
    """Download+re-encode one candidate's full image as JPEG, longest side
    capped at `max_side`. Returns None on any failure (dead link, blocked
    hotlinking, corrupt image) -> the caller drops that candidate."""
    if not url:
        return None
    try:
        resp = requests.get(url, timeout=timeout, headers=FETCH_HEADERS)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception:
        return None

    if max(img.size) > max_side:
        ratio = max_side / max(img.size)
        img = img.resize((max(1, round(img.width * ratio)), max(1, round(img.height * ratio))))

    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# --- Gemini call with retry/backoff -----------------------------------------

def _generate_with_retry(parts: list, model: str, max_retries: int = 4) -> dict:
    client = _client()
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_schema=SELECT_SCHEMA,
    )
    last_error = None
    for attempt in range(max_retries):
        try:
            resp = client.models.generate_content(model=model, contents=parts, config=config)
            return json.loads(resp.text)
        except errors.ClientError as e:
            if getattr(e, "code", None) == 429:
                last_error = e
                time.sleep(2 ** attempt)
                continue
            raise
        except errors.ServerError as e:
            last_error = e
            time.sleep(2 ** attempt)
            continue
    raise RuntimeError(f"Gemini fallo tras {max_retries} intentos: {last_error}")


# --- Post-Gemini rules --------------------------------------------
# The code doesn't re-choose: it only resolves the number Gemini returned to
# a real candidate and acts on the edge cases already described in the plan.

def _resolve(n: int | None, usable: list[dict]) -> tuple[int | None, dict | None]:
    if n is None:
        return None, None
    idx = n - 1
    if 0 <= idx < len(usable):
        return n, usable[idx]
    return None, None


def _postprocess(raw: dict, usable: list[dict]) -> dict:
    evaluaciones = raw.get("evaluaciones", []) or []
    ranking_n = raw.get("ranking", []) or []
    confianza = raw.get("confianza", "baja")
    comentario = raw.get("comentario", "")

    sel_n, seleccion = _resolve(raw.get("seleccion"), usable)
    # Gemini picked a number that doesn't resolve to a real candidate (out of
    # range) -> take the next one from its own ranking, no new call.
    if seleccion is None and raw.get("seleccion") is not None:
        for n in ranking_n:
            sel_n, seleccion = _resolve(n, usable)
            if seleccion is not None:
                break

    ranking_candidatos = [c for _, c in (_resolve(n, usable) for n in ranking_n) if c is not None]

    evaluaciones_by_n = {e.get("imagen"): e for e in evaluaciones}
    eval_sel = evaluaciones_by_n.get(sel_n) if seleccion is not None else None

    flags = []
    if seleccion is None:
        flags.append("sin_imagen")
    if seleccion is None or confianza == "baja":
        flags.append("necesita_fallback")   # etapa 3: next search rung
    if eval_sel and eval_sel.get("marca") == "no_verificable":
        flags.append("revisar_marca")

    return {
        "n_evaluadas": len(usable),
        "evaluaciones": evaluaciones,
        "confianza": confianza,
        "comentario": comentario,
        "seleccion_imagen": sel_n,
        "ranking_imagenes": ranking_n,
        "seleccion": seleccion,
        "ranking_candidatos": ranking_candidatos,
        "flags": flags,
    }


def _sin_candidatos(motivo: str) -> dict:
    return {
        "n_evaluadas": 0,
        "evaluaciones": [],
        "confianza": "baja",
        "comentario": motivo,
        "seleccion_imagen": None,
        "ranking_imagenes": [],
        "seleccion": None,
        "ranking_candidatos": [],
        "flags": ["sin_imagen", "necesita_fallback"],
    }


# --- Orchestration + per-product JSON cache ---------------------------------

def _safe(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(ref).strip()) or "unknown"


def _cache_path(ref: str) -> Path:
    return CACHE_DIR / f"{_safe(ref)}.json"


def select_product(ref: str, nombre_limpio: str, marca: str, categoria: str,
                presentacion: str, candidates: list[dict], model: str = MODEL_FLASH,
                use_cache: bool = True) -> dict:
    """Full Etapa 5 pipeline for one product's pre-filtered candidate pool.

    Returns (and caches to cache/select/{ref}.json) the resolved selection,
    ranking, confidence, and edge-case flags. `candidates` is the `kept` list
    from `prefilter.prefilter_product`.
    """
    path = _cache_path(ref)
    if use_cache and path.exists():
        return json.loads(path.read_text())

    if not candidates:
        result = _sin_candidatos("pool vacio tras el pre-filtro")
    else:
        images, usable, via_thumb = [], [], []
        for c in candidates:
            img_bytes = fetch_full_image(c.get("link", ""))
            # Original blocked (hotlink protection) -> Google's cached
            # thumbnail still works; low-res but enough for Gemini to judge.
            from_thumb = False
            if img_bytes is None:
                img_bytes = fetch_full_image(c.get("thumbnailLink", ""))
                from_thumb = img_bytes is not None
            if img_bytes is None:
                continue
            images.append(img_bytes)
            usable.append(c)
            via_thumb.append(from_thumb)

        if not usable:
            result = _sin_candidatos("ninguna imagen del pool se pudo descargar")
        else:
            prompt = build_prompt(nombre_limpio, marca, categoria, presentacion, len(usable))
            parts = [prompt] + [
                types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in images
            ]
            raw = _generate_with_retry(parts, model=model)
            result = _postprocess(raw, usable)
            sel_n = result.get("seleccion_imagen")
            if sel_n is not None and via_thumb[sel_n - 1]:
                result["flags"].append("descarga_thumbnail")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def select_df(df, filtrados: dict[str, dict], col_ref: str = "Ref Proveedor",
            col_marca: str = "Marca", col_nombre: str = "nombre_limpio",
            col_categoria: str = "categoria", col_presentacion: str = "presentacion",
            model: str = MODEL_FLASH, use_cache: bool = True, sleep: float = 1.0,
            max_calls: int = 40) -> dict[str, dict]:
    """Run select_product over every row (use on the golden set first).

    `filtrados` is the {ref: prefilter_result} dict from Etapa 4; only its
    `kept` list is used. `max_calls` caps NEW (uncached) Gemini calls, same
    cost-guard pattern as `search.search_df`. Returns {ref: result}.
    """
    results: dict[str, dict] = {}
    n_calls = 0
    n_skipped = 0
    for _, row in df.iterrows():
        ref = row[col_ref]
        kept = filtrados.get(ref, {}).get("kept", [])
        cached = use_cache and _cache_path(ref).exists()

        if not cached and n_calls >= max_calls:
            n_skipped += 1
            continue

        results[ref] = select_product(
            ref=ref,
            nombre_limpio=row.get(col_nombre, ""),
            marca=row[col_marca],
            categoria=row.get(col_categoria, ""),
            presentacion=row.get(col_presentacion, ""),
            candidates=kept,
            model=model,
            use_cache=use_cache,
        )
        if not cached:
            n_calls += 1
            if sleep:
                time.sleep(sleep)

    print(f"select: {n_calls} llamadas nuevas a Gemini (cap {max_calls}), "
        f"{len(results) - n_calls} desde cache, {n_skipped} omitidas por el cap")
    return results
