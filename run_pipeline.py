#!/usr/bin/env python3
"""
End-to-end product-image pipeline (DONREP)

What it does, per product, in order:
  1. Normalize the product name            (pipeline.normalize)
  2. Categorize it                          (pipeline.categorize)
  3. Search images on Google CSE            (pipeline.search)
  4. Pre-filter the candidate images        (pipeline.prefilter)
  5. Let Gemini pick the best image         (pipeline.select)
  6. Fallback search for the ones that failed (pipeline.fallback)
  7. Write everything to an Excel file with thumbnails (pipeline.io_excel)

Everything is CACHED on disk (see --cache-dir). Re-running the script does NOT
re-pay for API calls that already succeeded, it only does the work that is still
pending. This makes the run safe to stop and resume (important because the free
Google CSE tier only allows 100 new searches per day).

Usage:
    python run_pipeline.py                 # run the full catalog with defaults
    python run_pipeline.py --limit 5       # quick smoke test on the first 5 products
    python run_pipeline.py --help          # see every option

Requires a `.env` file and `credentials.json` in this folder (see the README).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

import pipeline.fallback as fallback_mod
import pipeline.io_excel as io_excel_mod
import pipeline.metrics as metrics_mod
import pipeline.prefilter as prefilter_mod
import pipeline.search as search_mod
import pipeline.select as select_mod
from pipeline.categorize import categorize_df
from pipeline.fallback import escalate_df
from pipeline.io_excel import build_output_df, write_excel
from pipeline.metrics import cost_report, selection_report
from pipeline.normalize import normalize_df
from pipeline.prefilter import prefilter_product
from pipeline.search import search_df
from pipeline.select import MODEL_FLASH, select_df

# Repo root = folder this script lives in (used for default paths and .env).
# Running `python run_pipeline.py` puts this dir on sys.path automatically, so
# the pipeline.* imports above resolve without any sys.path juggling.
ROOT = Path(__file__).resolve().parent

# Default column names in the input Excel.
COL_REF, COL_BRAND, COL_NAME = "Ref Proveedor", "Marca", "Nombre"


def log(msg: str) -> None:
    """Print a timestamped line so the operator can follow progress in the terminal."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #
def preflight(args) -> None:
    """Fail fast with a clear message if credentials or the input file are missing.

    We check this BEFORE spending any API calls so the operator does not wait for
    a run to die halfway through.
    """
    env_path = ROOT / ".env"
    if not env_path.exists():
        sys.exit(f"ERROR: no `.env` file found at {env_path}\n"
                 f"Copy `.env.example` to `.env` and fill in the prod credentials.")
    load_dotenv(env_path)

    required = ["GOOGLE_CSE_API_KEYS", "GOOGLE_CSE_CX",
                "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        sys.exit(f"ERROR: these variables are missing in `.env`: {', '.join(missing)}")

    # Vertex AI (Gemini) authenticates via a service-account key file pointed to by
    # GOOGLE_APPLICATION_CREDENTIALS. Resolve it relative to the repo and confirm it exists.
    cred = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
    cred_path = Path(cred)
    if not cred_path.is_absolute():
        cred_path = ROOT / cred_path
    if not cred_path.exists():
        sys.exit(f"ERROR: Google service-account key not found at {cred_path}\n"
                f"Set GOOGLE_APPLICATION_CREDENTIALS in `.env` to the prod key file.")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(cred_path)

    if not Path(args.input).exists():
        sys.exit(f"ERROR: input Excel not found at {args.input}")


def wire_cache_and_output(cache_root: Path, output_root: Path) -> None:
    """Point every pipeline module at the chosen cache/output folders.

    The pipeline modules keep their cache/output paths in module-level globals that
    are read at call time. We overwrite those globals here
    so we can isolate a prod run in its own folders without editing pipeline/*.py.
    """
    search_mod.CACHE_DIR = cache_root / "cse"
    prefilter_mod.CACHE_DIR = cache_root / "prefilter"
    select_mod.CACHE_DIR = cache_root / "select"
    fallback_mod.CACHE_DIR = cache_root / "fallback"
    metrics_mod.LEDGER = cache_root / "metrics" / "events.jsonl"
    io_excel_mod.IMG_DIR = output_root / "images"

    for d in [search_mod.CACHE_DIR, prefilter_mod.CACHE_DIR, select_mod.CACHE_DIR,
            fallback_mod.CACHE_DIR, metrics_mod.LEDGER.parent, io_excel_mod.IMG_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_products(input_xlsx: Path, limit: int | None):
    """Read the input Excel and return a clean DataFrame of products."""
    df = pd.read_excel(input_xlsx, dtype={COL_REF: str})
    df = df[[COL_REF, COL_BRAND, COL_NAME]].copy()
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()
    # Drop rows without a supplier reference (they are not valid as the literal search mixes the ref and the brand).
    df = df[df[COL_REF].notna() & (df[COL_REF] != "") & (df[COL_REF] != "nan")]
    df = df.reset_index(drop=True)
    if limit:
        df = df.head(limit).copy()
    return df


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the full product-image pipeline over the catalog.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", default=str(ROOT / "input" / "products.xlsx"),
                        help="Input Excel with the products.")
    parser.add_argument("--cache-dir", default=str(ROOT / "cache_prod"),
                        help="Folder for API-response cache (safe to reuse to resume a run).")
    parser.add_argument("--output-dir", default=str(ROOT / "output_prod"),
                        help="Folder for the final Excel and downloaded images.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N products (for a quick test).")
    parser.add_argument("--max-queries", type=int, default=None,
                        help="Cap on NEW Google CSE searches this run "
                            "(default: no cap = one per product). Lower it to stay under the 100/day free-tier limit.")
    parser.add_argument("--max-calls", type=int, default=None,
                        help="Cap on NEW Gemini calls this run (default: no cap).")
    parser.add_argument("--rung", type=int, default=1,
                        help="Initial CSE query style: 1='ref' 'brand' (recommended).")
    args = parser.parse_args()

    log("Checking credentials and input file...")
    preflight(args)

    cache_root = Path(args.cache_dir)
    output_root = Path(args.output_dir)
    wire_cache_and_output(cache_root, output_root)

    log(f"cache  -> {cache_root}")
    log(f"output -> {output_root}")

    # --- Stage 0: load ---
    df = load_products(Path(args.input), args.limit)
    log(f"Loaded {len(df)} products from {args.input}")

    # --- Stage 1-2: normalize + categorize ---
    log("Stage 1/2: normalizing names and categorizing...")
    df = normalize_df(df, col_nombre=COL_NAME, col_marca=COL_BRAND)
    df = categorize_df(df)

    n = len(df)
    max_queries = args.max_queries if args.max_queries is not None else n
    max_calls = args.max_calls if args.max_calls is not None else n

    # --- Stage 3: CSE image search ---
    log(f"Stage 3: searching images on Google CSE (up to {max_queries} new searches)...")
    resultados = search_df(df, rung=args.rung, max_queries=max_queries)

    # --- Stage 4: pre-filter (no API cost) ---
    log("Stage 4: pre-filtering candidate images...")
    filtrados = {ref: prefilter_product(ref, cands) for ref, cands in resultados.items()}

    # --- Stage 5: Gemini selection ---
    log(f"Stage 5: selecting the best image with Gemini (up to {max_calls} new calls)...")
    seleccionados = select_df(df, filtrados, model=MODEL_FLASH, max_calls=max_calls)

    # --- Stage 5b: fallback for products that failed ---
    log("Stage 5b: running fallback search for products with no image yet...")
    escalate_df(df, resultados, filtrados, seleccionados,
                max_queries=max_queries, max_calls=max_calls)

    # --- Stage 6: write Excel ---
    log("Stage 6: downloading chosen images and writing the Excel file...")
    out_df = build_output_df(df, seleccionados)
    out_xlsx = output_root / "catalogo_final.xlsx"
    write_excel(out_df, out_xlsx)

    # --- Summary ---
    log("Done. Summary:")
    _, resumen = selection_report(df, resultados, filtrados, seleccionados)
    con_imagen = int(resumen["con_imagen"])
    print(f"    Products processed : {int(resumen['productos'])}")
    print(f"    With an image      : {con_imagen} "
          f"({100 * con_imagen / max(1, int(resumen['productos'])):.0f}%)")
    print(f"    Without an image   : {int(resumen['sin_imagen'])}")
    print(f"    Output Excel       : {out_xlsx}")
    try:
        costo = cost_report()
        if not costo.empty:
            print("\n    Billable API usage this run (cache hits are free and not shown):")
            print(costo.to_string(index=False).replace("\n", "\n    "))
    except Exception:
        pass  # cost report is a nice-to-have; never fail the run over it.

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
