"""Etapa 1 - name normalization.

Produces the minimal search term per product, plus structured columns
extracted (not deleted) from the name. See plan.md Etapa 1.
"""
import unicodedata

import pandas as pd

from config.abbreviations import (
    ABBREVIATIONS,
    CASE_MULTIPLIER_RE,
    LITRAJE_RE,
    MEDIDA_LLANTA_RE,
    PACK_RE,
    TYPO_FIXES,
    VEHICULO_CODE_RE,
    VISCOSIDAD_RE,
)


def strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def clean_basic(nombre: str) -> str:
    text = str(nombre).strip().upper()
    text = " ".join(text.split())
    for pattern, replacement in TYPO_FIXES:
        text = pattern.sub(replacement, text)
    text = strip_accents(text)
    return text


def expand_abbreviations(text: str) -> str:
    tokens = [ABBREVIATIONS.get(tok, tok) for tok in text.split()]
    return " ".join(tokens)


def extract_pack(text: str) -> str:
    m = PACK_RE.search(text)
    return m.group(0).strip() if m else ""


def extract_litraje(text: str) -> str:
    m = LITRAJE_RE.search(text)
    return f"{m.group(1)}L".upper() if m else ""


def strip_case_multiplier(text: str) -> str:
    """Drop the case-count multiplier (X12UN, X4UND...) but keep the litraje
    marker it's attached to. No-op when there's no confirmed litraje, so
    ambiguous forms (4X4 UNID, 3X5, 6X1) are left untouched.
    """
    return CASE_MULTIPLIER_RE.sub(r"\1", text, count=1)


def extract_viscosidad(text: str) -> str:
    m = VISCOSIDAD_RE.search(text)
    return m.group(0).upper() if m else ""


def extract_medida_llanta(text: str) -> str:
    m = MEDIDA_LLANTA_RE.search(text)
    return m.group(0).upper() if m else ""


def extract_vehiculo_code(text: str) -> str:
    m = VEHICULO_CODE_RE.search(text)
    return m.group(1) if m else ""


def build_termino_busqueda(nombre_limpio: str, marca: str) -> str:
    """Minimal descriptive fallback query: nombre_limpio (already pack-free)
    plus brand if it's not already present.
    """
    term = nombre_limpio
    marca_norm = strip_accents(str(marca).strip().upper())
    if marca_norm and marca_norm not in term:
        term = f"{term} {marca_norm}".strip()
    return term


def normalize_row(nombre: str, marca: str) -> dict:
    nombre_limpio = clean_basic(nombre)
    nombre_limpio = expand_abbreviations(nombre_limpio)
    nombre_limpio = " ".join(nombre_limpio.split())

    pack = extract_pack(nombre_limpio)
    litraje = extract_litraje(nombre_limpio)

    if litraje:
        nombre_limpio = strip_case_multiplier(nombre_limpio)
        nombre_limpio = " ".join(nombre_limpio.split())

    # has a case-pack expression (PACK_RE) but no confirmed litraje to anchor
    # the strip to -> left untouched in nombre_limpio, needs manual review
    # (e.g. "4X4 UNID" - could be a mistyped "4LX4UND"?).
    pack_ambiguo = bool(pack) and not litraje

    return {
        "nombre_original": str(nombre).strip(),
        "nombre_limpio": nombre_limpio,
        "pack": pack,
        "litraje": litraje,
        "pack_ambiguo": pack_ambiguo,
        "viscosidad": extract_viscosidad(nombre_limpio),
        "medida_llanta": extract_medida_llanta(nombre_limpio),
        "vehiculo_code_raw": extract_vehiculo_code(nombre_limpio),
        "vehiculo_code": "",  # filled later via config/vehicle_codes.py mapping
        "termino_busqueda": build_termino_busqueda(nombre_limpio, marca),
    }


def normalize_df(df: pd.DataFrame, col_nombre: str = "Nombre", col_marca: str = "Marca") -> pd.DataFrame:
    records = [normalize_row(row[col_nombre], row[col_marca]) for _, row in df.iterrows()]
    return pd.concat([df.reset_index(drop=True), pd.DataFrame(records)], axis=1)
