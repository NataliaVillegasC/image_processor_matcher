"""Etapa 5b - fallback escalation over the search ladder (rungs 2-3).

Re-attacks only the products that came out of Etapa 5 without a usable image:
flags `sin_imagen` / `necesita_fallback` (seleccion=null, empty pool after the
pre-filter, or confianza baja). For each one it walks the remaining rungs of
the ladder defined in search.py (2 = unquoted, 3 = nombre_limpio), MERGES the
new candidates into the pool it already had (dedupe by link), EXCLUDES the
images a previous Gemini pass condemned on an eliminatory criterion (wrong
product / wrong brand -> see _condenadas; they're never re-downloaded nor
re-sent, and they can't crowd new candidates out of the prefilter's keep=6),
re-runs the pre-filter and a fresh Gemini selection, and stops at the first
rung whose result no longer needs fallback.

Cost model, same pattern as the rest of the pipeline:
  - The CSE call per rung is served by cache/cse/{ref}/rung{n}_... if present
    -> walking the ladder again does NOT re-pay searches.
  - prefilter/select run with use_cache=False (the pool changed, their per-ref
    cache is stale by definition) BUT the final escalated result is cached in
    cache/fallback/{ref}.json -> re-running the cell doesn't re-pay Gemini.
  - `max_queries` / `max_calls` cap NEW CSE / Gemini calls, same guard as
    search_df / select_df.

Every attempt is recorded in the returned log (ref, rung, pool sizes, outcome)
-> that's the metric for "how much did each extra rung actually rescue".
"""
import json
import re
import time
from pathlib import Path

import pandas as pd
import requests
from google.genai import errors

from pipeline.prefilter import prefilter_product
from pipeline.search import _cache_path as _search_cache_path
from pipeline.search import search_product
from pipeline.select import MODEL_FLASH, select_product

CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "fallback"

FALLBACK_RUNGS = (2, 3)


def needs_fallback(result: dict | None) -> bool:
    """A product needs another rung if Etapa 5 never ran for it or left it
    flagged sin_imagen / necesita_fallback."""
    if result is None:
        return True
    flags = result.get("flags", [])
    return "sin_imagen" in flags or "necesita_fallback" in flags


def _merge_pools(base: list[dict], extra: list[dict]) -> list[dict]:
    """Union by link, keeping the original order (rung 1 candidates first)."""
    seen = {c.get("link") for c in base}
    return base + [c for c in extra if c.get("link") not in seen]


def _condenadas(result: dict | None) -> set[str]:
    """Links that failed an ELIMINATORY criterion in a previous Gemini pass
    (wrong product / wrong brand) -> absolute verdicts about the image itself,
    safe to exclude from the next retry so they're never paid for again.

    Quality discards and marca=no_verificable are NOT included: those are
    relative to the pool they were judged in, and the same image can
    legitimately win against the weaker pool a lower rung brings.

    Select results cached BEFORE `candidatos_evaluados` existed can't map the
    image number back to a link -> empty set, nothing excluded (graceful)."""
    if not result:
        return set()
    links = result.get("candidatos_evaluados", [])
    out = set()
    for e in result.get("evaluaciones", []):
        n = e.get("imagen")
        if not isinstance(n, int) or not (1 <= n <= len(links)):
            continue
        if e.get("producto_correcto") is False or e.get("marca") == "incorrecta":
            out.add(links[n - 1])
    return out


def _serializable_pool(pool: list[dict]) -> list[dict]:
    """Pool without internal `_`-prefixed annotations (`_phash` is an
    ImageHash object -> not JSON-serializable). Belt and braces: prefilter no
    longer mutates the caller's pool, but a pool contaminated by an older
    session must still cache cleanly."""
    return [{k: v for k, v in c.items() if not k.startswith("_")} for c in pool]


def _safe(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(ref).strip()) or "unknown"


def _cache_path(ref: str) -> Path:
    return CACHE_DIR / f"{_safe(ref)}.json"


def escalate_df(df, resultados: dict, filtrados: dict, seleccionados: dict,
                col_ref: str = "Ref Proveedor", col_marca: str = "Marca",
                col_nombre: str = "nombre_limpio", col_categoria: str = "categoria",
                col_presentacion: str = "presentacion", col_profile: str = "cse_profile",
                rungs: tuple = FALLBACK_RUNGS, model: str = MODEL_FLASH,
                use_cache: bool = True, sleep: float = 1.0,
                max_queries: int = 20, max_calls: int = 20) -> pd.DataFrame:
    """Walk the fallback ladder for every product still flagged after Etapa 5.

    MUTATES `resultados` / `filtrados` / `seleccionados` in place with the
    escalated pool/prefilter/selection, so the Etapa 6 export cells work
    unchanged. Returns the attempt log as a DataFrame (one row per ref+rung
    tried, plus the final outcome per ref).

    Re-running with use_cache=True serves cache/fallback/{ref}.json -> free.
    Delete that file (or pass use_cache=False) after changing the prompt or
    the rungs to re-escalate a product.
    """
    log: list[dict] = []
    n_queries = 0
    n_calls = 0

    pendientes = [row for _, row in df.iterrows()
                if needs_fallback(seleccionados.get(row[col_ref]))]
    print(f"fallback: {len(pendientes)} productos a escalar (rungs {list(rungs)})")

    for row in pendientes:
        ref = row[col_ref]
        path = _cache_path(ref)

        if use_cache and path.exists():
            cached = json.loads(path.read_text())
            resultados[ref] = cached["pool"]
            filtrados[ref] = cached["prefilter"]
            seleccionados[ref] = cached["select"]
            log.extend(cached["log"])
            continue

        pool = list(resultados.get(ref, []))
        ref_log: list[dict] = []
        result = seleccionados.get(ref)
        # blacklist of links Gemini already condemned (wrong product / wrong
        # brand) -> excluded from every retry below, and grown after each new
        # verdict. Only excluded from the pool sent forward, NEVER deleted
        # from cache/cse: a Gemini false negative stays recoverable.
        excluidas = _condenadas(result)

        try:
            for rung in rungs:
                profile = row.get(col_profile, "baseline")
                cse_cached = _search_cache_path(ref, rung, profile).exists()
                if not cse_cached and n_queries >= max_queries:
                    ref_log.append({"ref": ref, "rung": rung, "resultado": "omitido_cap_cse"})
                    break

                nuevos = search_product(
                    ref=ref, marca=row[col_marca], profile=profile,
                    nombre_limpio=row.get(col_nombre, ""), rung=rung,
                )
                if not cse_cached:
                    n_queries += 1

                antes = sum(1 for c in pool if c.get("link") not in excluidas)
                pool = _merge_pools(pool, nuevos)
                candidatas = [c for c in pool if c.get("link") not in excluidas]
                if len(candidatas) == antes:
                    # the rung brought nothing that isn't already condemned or
                    # in the pool -> a Gemini call would re-judge the exact
                    # same images, skip straight to the next rung
                    ref_log.append({"ref": ref, "rung": rung, "n_nuevos": 0,
                                    "n_excluidas": len(excluidas),
                                    "resultado": "sin_candidatos_nuevos"})
                    continue

                prefiltrado = prefilter_product(ref, candidatas, use_cache=False)
                if not prefiltrado["kept"]:
                    ref_log.append({"ref": ref, "rung": rung,
                                    "n_nuevos": len(candidatas) - antes,
                                    "n_excluidas": len(excluidas),
                                    "n_kept": 0, "resultado": "prefiltro_vacio"})
                    filtrados[ref] = prefiltrado
                    resultados[ref] = pool
                    continue

                if n_calls >= max_calls:
                    ref_log.append({"ref": ref, "rung": rung, "resultado": "omitido_cap_gemini"})
                    break

                result = select_product(
                    ref=ref, nombre_limpio=row.get(col_nombre, ""), marca=row[col_marca],
                    categoria=row.get(col_categoria, ""),
                    presentacion=row.get(col_presentacion, ""),
                    candidates=prefiltrado["kept"], model=model, use_cache=False,
                )
                n_calls += 1
                if sleep:
                    time.sleep(sleep)

                resultados[ref] = pool
                filtrados[ref] = prefiltrado
                seleccionados[ref] = result
                resuelto = not needs_fallback(result)
                ref_log.append({
                    "ref": ref, "rung": rung, "n_nuevos": len(candidatas) - antes,
                    "n_excluidas": len(excluidas),
                    "n_kept": len(prefiltrado["kept"]),
                    "confianza": result.get("confianza"),
                    "flags": ", ".join(result.get("flags", [])) or "-",
                    "resultado": "resuelto" if resuelto else "sigue_pendiente",
                })
                if resuelto:
                    break
                # the fresh verdict may have condemned more images -> grow the
                # blacklist so the next rung doesn't pay for them again
                excluidas |= _condenadas(result)
        except (RuntimeError, errors.APIError, requests.RequestException) as e:
            # one product's failure (network blip, exhausted retries) must not
            # kill the whole escalation - log it and move to the next ref. No
            # fallback cache is written below, so the next run retries it.
            ref_log.append({"ref": ref, "rung": rung, "resultado": "error_red",
                            "flags": str(e)[:80]})
            print(f"  {ref}: rung {rung} fallo, se reintenta en la proxima corrida ({str(e)[:80]})")

        log.extend(ref_log)
        # cache the escalated state only if a Gemini call actually ran AND no
        # error cut the ladder short - a ref cut by the caps or by the network
        # must stay re-attemptable on the next run
        hubo_error = any(a.get("resultado") == "error_red" for a in ref_log)
        if not hubo_error and any(a.get("resultado") in ("resuelto", "sigue_pendiente") for a in ref_log):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "pool": _serializable_pool(resultados.get(ref, [])),
                "prefilter": filtrados.get(ref, {}),
                "select": seleccionados.get(ref, {}),
                "log": ref_log,
            }, ensure_ascii=False, indent=2))

    log_df = pd.DataFrame(log)
    if not log_df.empty:
        resueltos = (log_df["resultado"] == "resuelto").sum()
        print(f"fallback: {resueltos}/{len(pendientes)} productos rescatados, "
            f"{n_queries} queries CSE nuevas, {n_calls} llamadas Gemini nuevas")
    return log_df
