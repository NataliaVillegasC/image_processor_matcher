"""Pipeline metrics: cost (CSE + Gemini), retries, and output quality.

Event ledger at `cache/metrics/events.jsonl`: every NEW (uncached) call to the
CSE API or to Gemini appends one line, and so does every retry on a transient
error. Cache hits are NOT logged because they cost nothing -> the ledger
reflects real accumulated spend, not logical usage.

Three reports:
  - cost_report():      calls + tokens + estimated USD per service.
  - retry_report():     how many retries happened and on which error.
  - selection_report(): per-product funnel (CSE -> prefiltro -> evaluadas ->
                        seleccionada) + success rate / confianza / flags.

Prices are list-price constants (USD) - adjust here if Google changes them.
Note: select results cached BEFORE this instrumentation carry no token counts;
to backfill, delete cache/select and re-run (the Gemini call is paid again).
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

LEDGER = Path(__file__).resolve().parent.parent / "cache" / "metrics" / "events.jsonl"

# --- List prices (USD) -------------------------------------------------------

CSE_PRICE_PER_1000 = 5.0    # Custom Search JSON API, after the free tier
CSE_FREE_PER_DAY = 100

# USD per 1M tokens (Vertex AI, <=200k context). Thinking tokens of the 2.5
# models are billed as output.
GEMINI_PRICING = {
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00},
}


# --- Ledger ------------------------------------------------------------------

def log_event(kind: str, **fields) -> None:
    """Append one JSON line to the ledger. `kind`: cse_call | cse_retry |
    gemini_call | gemini_retry."""
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        **fields,
    }
    with LEDGER.open("a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_events() -> pd.DataFrame:
    if not LEDGER.exists():
        return pd.DataFrame(columns=["ts", "kind"])
    rows = [json.loads(line) for line in LEDGER.read_text().splitlines() if line.strip()]
    return pd.DataFrame(rows)


# --- Cost report ---------------------------------------------------------------

def cost_report() -> pd.DataFrame:
    """Calls, tokens and estimated USD per service, from the ledger.

    CSE is billed per query with 100 free PER DAY -> billable queries are
    computed day by day, not over the total.
    """
    ev = load_events()
    lines = []

    cse = ev[ev["kind"] == "cse_call"] if not ev.empty else pd.DataFrame()
    billable = 0
    if not cse.empty:
        per_day = pd.to_datetime(cse["ts"]).dt.date.value_counts()
        billable = int(sum(max(0, n - CSE_FREE_PER_DAY) for n in per_day))
    lines.append({
        "servicio": "CSE",
        "llamadas": len(cse),
        "tokens_entrada": None,
        "tokens_salida": None,
        "costo_usd": round(billable / 1000 * CSE_PRICE_PER_1000, 4),
        "nota": f"{billable} facturables tras {CSE_FREE_PER_DAY} gratis/dia",
    })

    gem = ev[ev["kind"] == "gemini_call"] if not ev.empty else pd.DataFrame()
    if not gem.empty:
        for model, g in gem.groupby("model"):
            tok_in = int(g["prompt_tokens"].fillna(0).sum())
            tok_out = int((g["output_tokens"].fillna(0) + g["thoughts_tokens"].fillna(0)).sum())
            price = GEMINI_PRICING.get(model)
            cost = (tok_in * price["input"] + tok_out * price["output"]) / 1e6 if price else float("nan")
            lines.append({
                "servicio": model,
                "llamadas": len(g),
                "tokens_entrada": tok_in,
                "tokens_salida": tok_out,
                "costo_usd": round(cost, 4),
                "nota": "salida incluye thinking tokens",
            })

    rep = pd.DataFrame(lines)
    print(f"Costo total estimado: ${rep['costo_usd'].sum():.4f} USD "
        f"({len(cse)} queries CSE, {len(gem)} llamadas Gemini)")
    return rep


def retry_report() -> pd.DataFrame:
    """Retries per service and error - if these grow, the backoff or the call
    rate (the `sleep` params of search_df/select_df) need tuning."""
    ev = load_events()
    retries = ev[ev["kind"].isin(["cse_retry", "gemini_retry"])] if not ev.empty else pd.DataFrame()
    if retries.empty:
        print("Sin reintentos registrados")
        return pd.DataFrame(columns=["kind", "error", "n"])
    return (retries.groupby(["kind", "error"]).size()
            .reset_index(name="n").sort_values("n", ascending=False))


# --- Output quality report -----------------------------------------------------

def selection_report(df, resultados: dict, filtrados: dict, seleccionados: dict,
                    col_ref: str = "Ref Proveedor") -> tuple[pd.DataFrame, dict]:
    """Per-product funnel + aggregate summary.

    Per product: how many candidates the CSE returned, how many survived the
    prefiltro, how many Gemini evaluated, and whether an image was selected.
    The summary says WHERE each product without an image was lost (empty CSE
    pool / empty prefiltro / Gemini discarded everything) -> that decides
    whether the fallback should attack the search or the prompt.
    """
    rows = []
    for _, row in df.iterrows():
        ref = row[col_ref]
        sel = seleccionados.get(ref) or {}
        rows.append({
            "ref": ref,
            "n_cse": len(resultados.get(ref, [])),
            "n_prefiltro": len(filtrados.get(ref, {}).get("kept", [])),
            "n_evaluadas": sel.get("n_evaluadas", 0),
            "seleccionada": sel.get("seleccion") is not None,
            "confianza": sel.get("confianza", "-"),
            "flags": ", ".join(sel.get("flags", [])) or "-",
        })
    per_ref = pd.DataFrame(rows)

    total = len(per_ref)
    con_imagen = int(per_ref["seleccionada"].sum())
    flag_counts = pd.Series(
        [f for fl in per_ref["flags"] for f in fl.split(", ") if f != "-"]
    ).value_counts().to_dict()

    resumen = {
        "productos": total,
        "con_imagen": con_imagen,
        "sin_imagen": total - con_imagen,
        "tasa_exito": round(con_imagen / total, 3) if total else 0.0,
        "confianza": per_ref["confianza"].value_counts().to_dict(),
        "flags": flag_counts,
        # where the products without an image were lost:
        "perdidos_pool_cse_vacio": int((per_ref["n_cse"] == 0).sum()),
        "perdidos_prefiltro_vacio": int(((per_ref["n_cse"] > 0) & (per_ref["n_prefiltro"] == 0)).sum()),
        "perdidos_gemini_descarto": int(((per_ref["n_prefiltro"] > 0) & (~per_ref["seleccionada"])).sum()),
    }

    print(f"{con_imagen}/{total} productos con imagen ({resumen['tasa_exito']:.0%})")
    print(f"  confianza: {resumen['confianza']}")
    print(f"  flags: {flag_counts}")
    print(f"  sin imagen por etapa: CSE vacio={resumen['perdidos_pool_cse_vacio']}, "
        f"prefiltro vacio={resumen['perdidos_prefiltro_vacio']}, "
        f"Gemini descarto todo={resumen['perdidos_gemini_descarto']}")
    return per_ref, resumen
