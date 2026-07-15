"""Etapa 3 - Google Custom Search (CSE) image search, with disk cache.

One call per product to control cost. The RAW CSE response is always saved to
`cache/cse/{ref}/rung{n}_{profile}[_geo].json` so re-runs of the rest of the
pipeline cost zero (the single most important cost decision of the MVP - see
plan.md Etapa 3). The filename encodes profile/geo because they change the
actual API params sent - without that, switching either would silently serve
a cached response fetched under different params.

Rung 1 (`"{ref}" "{marca}"`, both quoted) is the default and the most precise
query: it's the proven builder. The lower rungs of the fallback ladder are
defined here as functions so Etapas 4/5 can wire the escalation later, but this
module does NOT auto-advance rungs on its own - advancing is driven by a poor
pool after the pre-filter or a null Gemini selection, which don't exist yet.
"""
import json
import os
import re
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "cse"

# Market bias: Colombia / Spanish. Off by default (plan.md): probado con
# "8550504748" "MOTRIO" -> 0 resultados con gl=co&hl=es, 7 sin geo. Se deja
# como opt-in (geo=True) para volver a probarlo caso por caso.
GEO_PARAMS = {"gl": "co", "hl": "es"}

# Una sola clave para el MVP.
_API_KEY = os.getenv("GOOGLE_CSE_API_KEYS", "")
_CX = os.getenv("GOOGLE_CSE_CX", "")


# --- Query builders (the fallback ladder, plan.md Etapa 3) -----------------
# Only rung 1 is used by default. Lower rungs degrade filter strength step by
# step (most precise -> most lax) and are called by the escalation logic later.

def query_rung1(ref: str, marca: str, **_) -> str:
    """`"{ref}" "{marca}"` -> both quoted, literal. Most precise. The proven default."""
    return f'"{ref}" "{marca}"'


def query_rung2(ref: str, marca: str, **_) -> str:
    """`{ref} {marca}` -> unquoted. Rescues brands written differently (motrio,
    "Motrio by Renault") that the exact-phrase rung 1 would miss."""
    return f"{ref} {marca}"


def query_rung3(nombre_limpio: str, **_) -> str:
    """The clean product name -> descriptive fallback when ref-based rungs fail."""
    return nombre_limpio


QUERY_RUNGS = {1: query_rung1, 2: query_rung2, 3: query_rung3}


# --- CSE profile -> image params -------------------------------------------
# baseline stays minimal on purpose: imgType=photo (set unconditionally in
# cse_image_search) and the literal query, nothing else - imgSize=large was
# cutting results (see GEO_PARAMS note above for the same lesson with geo).
# Each category's cse_profile then adds its own bias on top of that minimal
# base. `exact_brand` needs the brand at call time, so it's applied here.

BASE_IMG_PARAMS = {}


def _profile_params(profile: str, marca: str) -> dict:
    params = dict(BASE_IMG_PARAMS)
    if profile == "white_dominant":
        params["imgDominantColor"] = "white"
    elif profile == "exact_brand":
        params["exactTerms"] = marca
    # baseline -> just the base
    return params


# --- Cache helpers ----------------------------------------------------------

def _safe(ref: str) -> str:
    """Filesystem-safe token from a ref (refs can carry slashes, spaces...)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(ref).strip()) or "unknown"


def _cache_path(ref: str, rung: int, profile: str = "baseline", geo: bool = False) -> Path:
    """profile/geo are part of the key because they change the actual API
    params sent - otherwise switching either would silently serve a response
    that was fetched under different params (see module docstring)."""
    suffix = "_geo" if geo else ""
    return CACHE_DIR / _safe(ref) / f"rung{rung}_{profile}{suffix}.json"


# --- Raw CSE call with retry/backoff ---------------------------------------

def cse_image_search(query: str, profile: str = "baseline", marca: str = "",
                    num: int = 10, geo: bool = False,
                    max_retries: int = 4) -> dict:
    """One raw CSE image search. Returns the parsed JSON dict.

    Backs off and retries on transient 5xx. Fails fast on quota/forbidden
    (429/403). Raises RuntimeError if no key or cx is configured.
    """
    if not _API_KEY:
        raise RuntimeError("GOOGLE_CSE_API_KEYS no configurada en .env")
    if not _CX:
        raise RuntimeError("GOOGLE_CSE_CX no configurada en .env")

    params = {
        "cx": _CX,
        "key": _API_KEY,
        "q": query,
        "searchType": "image",
        "num": num,
        "imgType": "photo",
        **(GEO_PARAMS if geo else {}),
        **_profile_params(profile, marca),
    }

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.get(CSE_ENDPOINT, params=params, timeout=30)
        except requests.RequestException as e:
            last_error = e
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            return resp.json()

        # quota / forbidden -> fail fast
        if resp.status_code in (429, 403):
            raise RuntimeError(
                f"CSE {resp.status_code} (cuota diaria agotada?): {resp.text[:200]}"
            )

        # transient server error -> backoff and retry
        if resp.status_code >= 500:
            last_error = RuntimeError(f"{resp.status_code}: {resp.text[:200]}")
            time.sleep(2 ** attempt)
            continue

        # 4xx we can't recover from (bad query, bad cx...) -> fail fast
        resp.raise_for_status()

    raise RuntimeError(f"CSE fallo tras {max_retries} intentos: {last_error}")


# --- Parse candidates from a raw response ----------------------------------

def candidates_from_response(raw: dict) -> list[dict]:
    """Pull the fields Etapa 4 needs (all free from the CSE response)."""
    items = raw.get("items", []) or []
    out = []
    for it in items:
        img = it.get("image", {}) or {}
        out.append({
            "link": it.get("link", ""),
            "title": it.get("title", ""),
            "width": img.get("width"),
            "height": img.get("height"),
            "thumbnailLink": img.get("thumbnailLink", ""),
            "displayLink": it.get("displayLink", ""),
        })
    return out


# --- Cached per-product search ---------------------------------------------

def search_product(ref: str, marca: str, profile: str = "baseline",
                nombre_limpio: str = "", rung: int = 1,
                use_cache: bool = True, geo: bool = False) -> list[dict]:
    """Cached CSE search for one product at a given rung.

    Reads `cache/cse/{ref}/rung{n}_{profile}[_geo].json` if present; otherwise
    calls the API and writes the RAW response there. Returns parsed candidates.
    """
    path = _cache_path(ref, rung, profile, geo)
    if use_cache and path.exists():
        raw = json.loads(path.read_text())
        return candidates_from_response(raw)

    query = QUERY_RUNGS[rung](ref=ref, marca=marca, nombre_limpio=nombre_limpio)
    raw = cse_image_search(query, profile=profile, marca=marca, geo=geo)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    return candidates_from_response(raw)


def search_df(df, col_ref: str = "Ref Proveedor", col_marca: str = "Marca",
            col_profile: str = "cse_profile", col_nombre: str = "nombre_limpio",
            rung: int = 1, use_cache: bool = True, geo: bool = False,
            sleep: float = 0.5, max_queries: int = 40) -> dict[str, list[dict]]:
    """Run search_product over every row (use on the golden set first).

    `max_queries` caps NEW (uncached) API calls to protect the free daily budget
    (100/day). Cached rows are always served; once the cap is reached, remaining
    uncached rows are skipped (not in the result) instead of spending queries.
    Returns {ref: [candidates]}. Sleeps between uncached calls only.
    """
    results: dict[str, list[dict]] = {}
    n_calls = 0
    n_skipped = 0
    for _, row in df.iterrows():
        ref = row[col_ref]
        profile = row.get(col_profile, "baseline")
        cached = use_cache and _cache_path(ref, rung, profile, geo).exists()

        if not cached and n_calls >= max_queries:
            n_skipped += 1
            continue

        results[ref] = search_product(
            ref=ref,
            marca=row[col_marca],
            profile=profile,
            nombre_limpio=row.get(col_nombre, ""),
            rung=rung,
            use_cache=use_cache,
            geo=geo,
        )
        if not cached:
            n_calls += 1
            if sleep:
                time.sleep(sleep)

    print(f"rung {rung}: {n_calls} queries nuevas a la API (cap {max_queries}), "
        f"{len(results) - n_calls} desde cache, {n_skipped} omitidas por el cap")
    return results
