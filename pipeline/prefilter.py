"""Etapa 4 - cheap pre-filter (no Gemini).

Cuts the raw CSE pool (Etapa 3) down to the handful of candidates that are
worth a Gemini call: drops exact duplicates (by URL) and visual duplicates
(same photo served from a different domain, via perceptual hash), low
resolution, banner-shaped aspect ratios, and blocklisted domains; then SORTS
(does not drop) by priority domain and white-background score; then verifies
the first `keep` survivors actually respond (HEAD, no full-body download).

No full-size images are downloaded or saved here -> Gemini (Etapa 5) fetches
them itself, in memory, at call time. The only thing written to disk is the
per-product result JSON, same caching pattern as cache/cse in search.py.

Thumbnails ARE fetched (they're what the CSE gives you for exactly this) but
only in memory, to score the background and compute the hash.
"""
import json
import re
from io import BytesIO
from pathlib import Path

import imagehash
import numpy as np
import requests
from PIL import Image

from config.domains import BLOCKLIST, PRIORITY_DOMAINS

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "prefilter"

MIN_SIDE = 400          # below this, drop (adjustable)
MAX_ASPECT_RATIO = 3.0  # above this in either direction, likely a banner
HASH_MAX_DISTANCE = 4   # phash: <=4 is treated as the same photo


# --- Step 1: exact dedupe (URL / thumbnailLink) -----------------------------

def dedupe_by_url(candidates: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    seen_link, seen_thumb = set(), set()
    kept, dropped = [], []
    for c in candidates:
        link, thumb = c.get("link", ""), c.get("thumbnailLink", "")
        if link in seen_link or (thumb and thumb in seen_thumb):
            dropped.append((c, "duplicado_url"))
            continue
        seen_link.add(link)
        if thumb:
            seen_thumb.add(thumb)
        kept.append(c)
    return kept, dropped


# --- Steps 2-4: hard filters (drop) -----------------------------------------

def _domain_blocked(domain: str) -> bool:
    domain = (domain or "").lower()
    return any(bad in domain for bad in BLOCKLIST)


def apply_hard_filters(candidates: list[dict], min_side: int = MIN_SIDE,
                    max_ratio: float = MAX_ASPECT_RATIO
                    ) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Low resolution, extreme aspect ratio, blocklisted domain.

    Each drop keeps its reason so it can be audited later -> same pattern as
    coverage_report in categorize.py.
    """
    kept, dropped = [], []
    for c in candidates:
        w, h = c.get("width") or 0, c.get("height") or 0
        domain = c.get("displayLink", "")
        if min(w, h) < min_side:
            dropped.append((c, f"resolucion_baja {w}x{h}"))
        elif max(w, h) / max(min(w, h), 1) > max_ratio:
            dropped.append((c, f"aspect_ratio_extremo {w}x{h}"))
        elif _domain_blocked(domain):
            dropped.append((c, f"dominio_bloqueado {domain}"))
        else:
            kept.append(c)
    return kept, dropped


# --- Step 5: in-memory thumbnail -> background score + visual hash ---------

def fetch_thumbnail(url: str, timeout: int = 10) -> Image.Image | None:
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return Image.open(BytesIO(resp.content)).convert("RGB")
    except Exception:
        return None


def _border_samples(img: Image.Image) -> np.ndarray:
    """4 corners + the midpoint of each side = 8 border samples."""
    w, h = img.size
    xs, ys = [0, w // 2, w - 1], [0, h // 2, h - 1]
    points = [(x, y) for x in xs for y in ys if x in (0, w - 1) or y in (0, h - 1)]
    arr = np.asarray(img)
    return np.array([arr[y, x] for x, y in points], dtype=float)


def background_score(img: Image.Image, white_thr: float = 235) -> float:
    """0-1: how white AND how uniform the thumbnail's border is.

    Soft ORDERING signal, not a hard filter -> a dark
    monochromatic background is still an acceptable fallback.
    """
    samples = _border_samples(img)
    whiteness = float((samples.min(axis=1) >= white_thr).mean())
    uniformity = 1.0 - min(float(samples.std(axis=0).mean()) / 128.0, 1.0)
    return round(0.7 * whiteness + 0.3 * uniformity, 3)


def score_candidates(candidates: list[dict]) -> list[dict]:
    """Fetch each thumbnail ONCE (in memory) and annotate `bg_score` and
    `_phash`. Candidates whose thumbnail can't be fetched get bg_score=0 and
    _phash=None (not dropped here -> the hard filter already passed).

    Annotates COPIES: the caller's dicts are never mutated. `_phash` is an
    ImageHash object -> leaking it into the caller's pool makes that pool
    non-JSON-serializable (fallback.py caches the pool to disk)."""
    out = []
    for c in candidates:
        c = dict(c)
        img = fetch_thumbnail(c.get("thumbnailLink", ""))
        c["bg_score"] = background_score(img) if img else 0.0
        c["_phash"] = imagehash.phash(img) if img else None
        out.append(c)
    return out


def dedupe_by_hash(candidates: list[dict], max_distance: int = HASH_MAX_DISTANCE
                    ) -> tuple[list[dict], list[tuple[dict, str]]]:
    """The same photo served from two different domains has a different
    thumbnailLink but identical visual content. Compare Hamming distance of
    the phash already computed in score_candidates (no extra network call).
    Among near-duplicates, keep the higher-resolution one."""
    ordered = sorted(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0),
                    reverse=True)
    kept, dropped, seen_hashes = [], [], []
    for c in ordered:
        h = c.get("_phash")
        if h is not None and any(h - seen <= max_distance for seen in seen_hashes):
            dropped.append((c, "duplicado_visual"))
            continue
        if h is not None:
            seen_hashes.append(h)
        kept.append(c)
    return kept, dropped


# --- Steps 6-7: soft prioritization + trim ----------------------------------

def _is_priority(domain: str) -> bool:
    domain = (domain or "").lower()
    return any(p in domain for p in PRIORITY_DOMAINS)


def rank(candidates: list[dict]) -> list[dict]:
    """Priority domain first, then bg_score. Drops nothing."""
    return sorted(
        candidates,
        key=lambda c: (_is_priority(c.get("displayLink", "")), c.get("bg_score", 0)),
        reverse=True,
    )


# --- Reachability check (no full-body download) -----------------------------

def check_reachable(url: str, timeout: int = 10) -> bool:
    """HEAD first. The full image is never read/saved here."""
    try:
        head = requests.head(url, timeout=timeout, allow_redirects=True)
        if head.status_code < 400:
            return True
        if head.status_code in (403, 405):
            with requests.get(url, timeout=timeout, stream=True) as get:
                return get.status_code < 400
        return False
    except requests.RequestException:
        return False


# --- Orchestration + per-product JSON cache ---------------------------------

def _safe(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(ref).strip()) or "unknown"


def _cache_path(ref: str) -> Path:
    return CACHE_DIR / f"{_safe(ref)}.json"


def _public(c: dict) -> dict:
    """Copy of a candidate without the internal `_phash` field (not JSON-serializable)."""
    return {k: v for k, v in c.items() if not k.startswith("_")}


def prefilter_product(ref: str, candidates: list[dict], keep: int = 6,
                    min_side: int = MIN_SIDE, max_ratio: float = MAX_ASPECT_RATIO,
                    use_cache: bool = True) -> dict:
    """Full Etapa 4 pipeline for one product.

    Returns (and caches to cache/prefilter/{ref}.json):
      - kept: up to `keep` candidates verified reachable, with `bg_score` for
        inspection/ordering.
      - dropped: [(candidate, reason)] from dedupe + hard filters (auditable).
      - n_input: size of the raw pool received.
    """
    path = _cache_path(ref)
    if use_cache and path.exists():
        return json.loads(path.read_text())

    deduped, dropped_url = dedupe_by_url(candidates)
    hard_kept, dropped_hard = apply_hard_filters(deduped, min_side, max_ratio)
    scored = score_candidates(hard_kept)
    deduped_visual, dropped_visual = dedupe_by_hash(scored)
    ranked = rank(deduped_visual)

    kept = []
    for c in ranked:
        if len(kept) >= keep:
            break
        if check_reachable(c.get("link", "")):
            kept.append(_public(c))

    result = {
        "kept": kept,
        "dropped": [(_public(c), reason) for c, reason in
                    dropped_url + dropped_hard + dropped_visual],
        "n_input": len(candidates),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
