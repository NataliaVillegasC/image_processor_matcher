"""
Car-parts image pipeline
Phase 1: Google CSE image extraction (with caching)
Phase 2: cheap filtering + Gemini ranking

Run cell-by-cell in Jupyter (use # %% markers) or as a script.
Requires: requests pandas openpyxl python-dotenv pillow imagehash numpy google-genai
"""

# %% ---------------------------------------------------------------- setup
import os, re, json, time, hashlib
from io import BytesIO
from pathlib import Path

import requests
import pandas as pd
import numpy as np
from PIL import Image
from dotenv import load_dotenv

load_dotenv()
CSE_KEY = os.environ["GOOGLE_CSE_API_KEYS"]
CSE_CX  = os.environ["GOOGLE_CSE_CX"]

CACHE_FILE = Path("cse_cache.json")   # so you never pay twice for the same query
POOL_FILE  = Path("pool.json")

_cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}

def _save_cache():
    CACHE_FILE.write_text(json.dumps(_cache, ensure_ascii=False, indent=1))

# %% ------------------------------------------------- 1. load the Excel
def load_parts(xlsx_path: str) -> pd.DataFrame:
    df = pd.read_excel(xlsx_path)
    df.columns = [c.strip() for c in df.columns]
    # expected: 'Ref Proveedor', 'Marca', 'Nombre'
    return df

# %% ------------------------------------------------- 2. build the query
PRESENTATION_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(L|LT|LTS|ML|GAL)\b", re.I)
PACK_RE = re.compile(r"X\s*\d+\s*UN[DI]*", re.I)   # 1LX12UND -> the X12UND part

def build_query(row) -> dict:
    """Return {'query': str, 'brand': str, 'presentation': str}"""
    ref = str(row["Ref Proveedor"]).strip()
    name  = str(row["Nombre"])
    brand = str(row["Marca"]).strip()

    m = PRESENTATION_RE.search(name)
    presentation = (m.group(1) + m.group(2).upper()) if m else ""

    clean = PACK_RE.sub("", name)           # drop 'X12UND' (box count, not photo!)
    clean = re.sub(r"\s+", " ", clean).strip()

    # the brand is usually already in the name; avoid doubling it
    q = f'"{ref}" "{brand}"'
    return {"query": q, "brand": brand, "presentation": presentation}

# %% ------------------------------------------------- 3. one CSE call
def cse_image_search(query: str, start: int = 1, **extra) -> list[dict]:
    """One API call. extra = imgSize, imgDominantColor, imgType, gl, hl, ..."""
    params = {
        "key": CSE_KEY, "cx": CSE_CX, "q": query,
        "searchType": "image", "num": 10, "imgType": "photo", "start": start,
        **extra,
    }
    cache_key = json.dumps({k: v for k, v in params.items() if k != "key"},
                        sort_keys=True, ensure_ascii=False)
    if cache_key in _cache:
        return _cache[cache_key]

    for attempt in range(3):
        r = requests.get("https://www.googleapis.com/customsearch/v1",
                        params=params, timeout=30)
        if r.status_code == 429:            # quota / rate limit -> back off
            time.sleep(5 * (attempt + 1)); continue
        r.raise_for_status()
        break
    else:
        # exhausted all retries still 429 -> don't cache, don't pretend it's 0 results
        raise RuntimeError(f"CSE rate-limited 3x in a row for query={query!r}")

    items = []
    for it in r.json().get("items", []):
        img = it.get("image", {})
        items.append({
            "url":        it.get("link"),          # <- the deliverable URL
            "title":      it.get("title", ""),
            "snippet":    it.get("snippet", ""),
            "domain":     it.get("displayLink", ""),
            "context":    img.get("contextLink", ""),
            "mime":       it.get("mime", ""),
            "width":      img.get("width", 0),
            "height":     img.get("height", 0),
            "byte_size":  img.get("byteSize", 0),
            "thumb":      img.get("thumbnailLink", ""),
        })
    _cache[cache_key] = items
    _save_cache()
    return items

# %% ------------------------------------------------- 4. pool per part
BASE_FILTERS = dict(imgSize="large", imgDominantColor="white",
                    imgType="photo")

def get_pool(row, target: int = 20) -> dict:
    meta = build_query(row)
    q = meta["query"]
    pool, seen = [], set()

    def add(items):
        for it in items:
            if it["url"] and it["url"] not in seen:
                seen.add(it["url"]); pool.append(it)

    # page 1 + 2 with strict filters
    add(cse_image_search(q, start=1,  **BASE_FILTERS))
    add(cse_image_search(q, start=11, **BASE_FILTERS))

    # relax filters if the part is rare and we got too few
    if len(pool) < target // 2:
        relaxed = {k: v for k, v in BASE_FILTERS.items() if k not in ("imgDominantColor", "imgSize")}
        add(cse_image_search(q, start=1, **relaxed))

    # last resort: drop every optional filter, keep only imgType
    if len(pool) < target // 2:
        add(cse_image_search(q, start=1, imgType="photo"))

    return {"ref": str(row["Ref Proveedor"]), **meta, "candidates": pool[:target]}

# %% ------------------------------------------------- 5. run extraction
df = load_parts("/Users/nataliavillegas/Documents/FUTURE/DONREP/image_processor_matcher/productos_renault.xlsx")
pools = [get_pool(row) for _, row in df.head(10).iterrows()]
POOL_FILE.write_text(json.dumps(pools, ensure_ascii=False, indent=1))

# %% ------------------------------- Jupyter helper: eyeball the pool
def show_pool(pool):
    """Display thumbnails inline in Jupyter."""
    from IPython.display import display, HTML
    cells = "".join(
        f"<div style='display:inline-block;margin:4px;text-align:center;width:130px'>"
        f"<img src='{c['thumb']}' style='max-width:120px'><br>"
        f"<small>{c['width']}x{c['height']}<br>{c['domain']}</small></div>"
        for c in pool["candidates"])
    display(HTML(f"<h4>{pool['ref']} — {pool['query']}</h4>{cells}"))

show_pool(pools[0])   # example
# ==================================================================
# PHASE 2 — SELECTION
# ==================================================================

# %% ------------------------------------------- Stage A: metadata filters
BAD_DOMAINS = {"alamy.com", "shutterstock.com", "dreamstime.com",
               "123rf.com", "istockphoto.com", "pinterest.com"}
RIVAL_BRANDS = {"castrol", "mobil", "shell", "valvoline", "motul",
                "total", "terpel", "havoline", "brembo", "trw", "ferodo"}

def stage_a(pool, min_side=300):
    brand = pool["brand"].lower()
    kept, dropped = [], []
    for c in pool["candidates"]:
        text = f"{c['title']} {c['snippet']} {c['context']}".lower()
        reason = None
        if min(c["width"], c["height"]) < min_side:
            reason = f"low_res {c['width']}x{c['height']}"
        elif not (0.6 <= c["width"] / max(c["height"], 1) <= 1.7):
            reason = "bad_aspect_ratio"
        elif any(d in c["domain"] for d in BAD_DOMAINS):
            reason = f"bad_domain {c['domain']}"
        elif brand not in text:
            reason = "brand_not_in_text"
        else:
            # OEM/dealer parts (e.g. Renault-branded oil made by Castrol/Motrio) legitimately
            # name a "rival" brand once; only reject pages comparing several at once.
            rivals_found = [rb for rb in RIVAL_BRANDS - {brand} if rb in text]
            if len(rivals_found) >= 2:
                reason = f"multi_rival_brand {rivals_found}"
        (dropped if reason else kept).append((c, reason))
    pool["stage_a"] = [c for c, _ in kept]
    pool["stage_a_dropped"] = [(c["url"], r) for c, r in dropped]  # read these logs!
    return pool



show_pool(stage_a(pools[0])) # example
# %% ---------------------------------- Stage B: thumbnail pixel checks
import imagehash

def fetch_thumb(url) -> Image.Image | None:
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGB")
    except Exception:
        return None

def border_whiteness(img: Image.Image, band=3, thr=235) -> float:
    """Fraction of border pixels that are near-white. ~1.0 => white background."""
    a = np.asarray(img)
    border = np.concatenate([a[:band].reshape(-1, 3), a[-band:].reshape(-1, 3),
                             a[:, :band].reshape(-1, 3), a[:, -band:].reshape(-1, 3)])
    return float((border.min(axis=1) >= thr).mean())

def border_monochrome(img: Image.Image, band=3, std_thr=18) -> bool:
    a = np.asarray(img)
    border = np.concatenate([a[:band].reshape(-1, 3), a[-band:].reshape(-1, 3),
                             a[:, :band].reshape(-1, 3), a[:, -band:].reshape(-1, 3)])
    return bool(border.std(axis=0).mean() < std_thr)

def stage_b(pool, keep=6):
    scored, hashes = [], set()
    for c in pool["stage_a"]:
        img = fetch_thumb(c["thumb"])
        if img is None:
            continue
        h = imagehash.phash(img)
        if any(h - other <= 4 for other in hashes):   # near-duplicate
            continue
        hashes.add(h)
        c["bg_white"] = round(border_whiteness(img), 2)
        c["bg_mono"]  = border_monochrome(img)
        c["_img"] = img                                # keep for Gemini stage
        scored.append(c)
    scored.sort(key=lambda c: (c["bg_white"], c["width"] * c["height"]), reverse=True)
    pool["stage_b"] = scored[:keep]
    return pool

# %% ------------------------------------------- Stage C: Gemini ranking
from google import genai
from google.genai import types

gclient = genai.Client(vertexai=True,
                        project=os.environ["GOOGLE_CLOUD_PROJECT"],
                        location=os.environ["GOOGLE_CLOUD_LOCATION"])
GEMINI_MODEL = "gemini-2.5-flash-lite"   # cheap tier; bump to 2.5-flash if quality lacks

SCHEMA = types.Schema(
    type="OBJECT",
    properties={
        "evaluations": types.Schema(type="ARRAY", items=types.Schema(
            type="OBJECT",
            properties={
                "index":            types.Schema(type="INTEGER"),
                "correct_brand":    types.Schema(type="BOOLEAN"),
                "correct_quantity": types.Schema(type="BOOLEAN"),
                "white_or_mono_bg": types.Schema(type="BOOLEAN"),
                "new_condition":    types.Schema(type="BOOLEAN"),
                "not_packaging_only": types.Schema(type="BOOLEAN"),
                "score":            types.Schema(type="INTEGER"),  # 0-100
            },
            required=["index", "correct_brand", "correct_quantity",
                      "white_or_mono_bg", "new_condition",
                      "not_packaging_only", "score"])),
        "best_index": types.Schema(type="INTEGER"),
    },
    required=["evaluations", "best_index"])

PROMPT = """Eres un evaluador de fotos de producto para un catálogo de repuestos de autos.
Producto: {query}
Marca requerida: {brand}
Presentación requerida: {presentation}

Evalúa cada imagen (numeradas desde 0) según:
1. correct_brand: la marca visible corresponde EXACTAMENTE a {brand}. Si se ve otra marca, false.
2. correct_quantity: la foto muestra la presentación indicada ({presentation}).
   Para pastillas de freno: 2 pastillas traseras = correcto; 1 pastilla = aceptable;
   4 pastillas = SIEMPRE false.
3. white_or_mono_bg: fondo blanco, o en su defecto monocromático.
4. new_condition: el producto se ve nuevo, sin uso ni desgaste.
5. not_packaging_only: la foto NO es solo caja/bolsa/empaque. Se permite que la caja
   aparezca junto al producto si ayuda a verificar la marca.

score: calidad general 0-100 para foto principal de catálogo (nitidez, encuadre, resolución aparente).
best_index: el índice de la mejor imagen que cumpla los criterios."""

def stage_c(pool):
    cands = pool.get("stage_b", [])
    if not cands:
        pool["winner"] = None; return pool

    parts = [PROMPT.format(query=pool["query"], brand=pool["brand"],
                           presentation=pool["presentation"] or "la del nombre del producto")]
    for c in cands:
        buf = BytesIO(); c["_img"].save(buf, format="JPEG", quality=80)
        parts.append(types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"))

    resp = gclient.models.generate_content(
        model=GEMINI_MODEL, contents=parts,
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=SCHEMA))
    result = json.loads(resp.text)

    # HARD disqualifiers in code — never trust a scalar score alone
    ranked = sorted(
        (e for e in result["evaluations"]
         if e["correct_brand"] and e["correct_quantity"] and e["not_packaging_only"]),
        key=lambda e: (e["white_or_mono_bg"], e["new_condition"], e["score"]),
        reverse=True)

    pool["gemini"] = result
    pool["ranked_urls"] = [cands[e["index"]]["url"] for e in ranked if e["index"] < len(cands)]
    pool["winner"] = pool["ranked_urls"][0] if pool["ranked_urls"] else None
    return pool

# %% ------------------------------------------- Stage D: URL still alive?
def validate_winner(pool):
    for url in pool.get("ranked_urls", []):
        try:
            if requests.head(url, timeout=10, allow_redirects=True).status_code == 200:
                pool["winner"] = url; return pool
        except Exception:
            continue
    pool["winner"] = None
    return pool

# %% ------------------------------------------- full run example
df = load_parts("/Users/nataliavillegas/Documents/FUTURE/DONREP/image_processor_matcher/productos_renault.xlsx")
results = []
for _, row in df.head(10).iterrows():
    p = get_pool(row)
    p = stage_a(p); p = stage_b(p); p = stage_c(p); p = validate_winner(p)
    results.append({"Ref Proveedor": p["ref"], "url_foto_primaria": p["winner"],
                    "alternativas": ";".join(p.get("ranked_urls", [])[1:3])})
    print(p["ref"], "->", p["winner"])
pd.DataFrame(results).to_excel("resultado_fotos.xlsx", index=False)
# %%
show_pool(stage_a(pools[4])) # example
# %%
