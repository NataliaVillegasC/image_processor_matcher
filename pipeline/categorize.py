"""Etapa 2 - keyword-first category classifier.

First-match-wins over config.categories.CATEGORIAS (order matters there).
Anything unmatched lands in `otros` and is surfaced by coverage_report — that
list is the backlog for new rules.
"""
import re
import pandas as pd
from config.categories import CATEGORIAS, OTROS, Categoria


def _kw_pattern(keyword: str) -> re.Pattern:
    """Prefix match at a word start: the keyword must not be glued to a LETTER
    on its LEFT, but it CAN be followed by more letters. Multi-word keywords match their
    spaces literally.
    """
    return re.compile(rf"(?<![A-Za-z]){re.escape(keyword)}")


# Precompile every keyword once: [(Categoria, [compiled patterns]), ...]
_COMPILED: list[tuple[Categoria, list[re.Pattern]]] = [
    (cat, [_kw_pattern(kw) for kw in cat.keywords]) for cat in CATEGORIAS
]


def categorize_name(nombre_limpio: str) -> Categoria:
    """Return the first Categoria whose any keyword matches, else OTROS."""
    text = str(nombre_limpio).upper()
    for cat, patterns in _COMPILED:
        if any(p.search(text) for p in patterns):
            return cat
    return OTROS


def categorize_df(df: pd.DataFrame, col: str = "nombre_limpio") -> pd.DataFrame:
    """Add categoria / cse_profile / presentacion columns to a normalized df."""
    cats = [categorize_name(v) for v in df[col]]
    out = df.copy()
    out["categoria"] = [c.nombre for c in cats]
    out["cse_profile"] = [c.cse_profile for c in cats]
    out["presentacion"] = [c.presentacion for c in cats]
    return out


def coverage_report(df: pd.DataFrame, col: str = "nombre_limpio") -> pd.DataFrame:
    """Print a per-category count + the `otros` rows (the rules backlog),
    and return the counts as a DataFrame for inspection in the notebook.
    """
    catted = categorize_df(df, col)
    counts = catted["categoria"].value_counts()
    total = len(catted)

    print(f"Cobertura: {total} filas, {catted['categoria'].nunique()} categorias\n")
    for nombre, n in counts.items():
        print(f"  {n:3d}  {nombre}")

    otros = catted.loc[catted["categoria"] == "otros", col].tolist()
    n_otros = len(otros)
    print(f"\n'otros' (backlog para nuevas reglas): {n_otros} "
        f"({n_otros / total:.0%})")
    for name in sorted(otros):
        print(f"    - {name}")

    return counts.rename_axis("categoria").reset_index(name="n")
