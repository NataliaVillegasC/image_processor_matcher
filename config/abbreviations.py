"""Regex fixes and abbreviation expansion for product names (Etapa 1).

Built by inspecting the real 281 `Nombre` values in input/products.xlsx, not
guessed abstractly. Grows as new abbreviations show up in future catalogs.
"""
import re

# Applied first, on the raw uppercased string, to fix known duplication/typos.
# !!!!Order matters, applied in sequence.!!!!
TYPO_FIXES = [
    (re.compile(r"\bDELANTEROANTERO\b"), "DELANTERO"),
    (re.compile(r"\bUNID\b"), "UND"),
    (re.compile(r"\bJGO\b"), "JUEGO"),
]

# Applied token-by-token (whitespace-split) after typo fixes and accent
# stripping, so it never touches brand names like MOTRIO (one token) or
# collides with substrings.
ABBREVIATIONS = {
    "DEL": "DELANTERO",
    "DELANT": "DELANTERO",
    "TRAS": "TRASERO",
    "TRA": "TRASERO",
    "IZ": "IZQUIERDO",
    "IZQ": "IZQUIERDO",
    "IZQUIER": "IZQUIERDO",
    "DER": "DERECHO",
    "DR": "DERECHO",
    "AMOR": "AMORTIGUADOR",
    "MOT": "MOTOR",
    "PTAL": "PUERTA LATERAL",
    "FILT": "FILTRO",
    "FILTR": "FILTRO",
    "ACEIT": "ACEITE",
    "RETROV": "RETROVISOR",
    "RETRO": "RETROVISOR",
    "INF": "INFERIOR",
    "PLUMIL": "PLUMILLA",
    "CORR": "CORREA",
    "CALAN": "CALANDRIA",
}

# ---------- Structured token extraction (kept, not deleted, from nombre_limpio) ----------

# Broad detector for "this row has a case-pack expression" (1LX12UN, 4LX4UND,
# 4X4 UNID, 3X5, 6X1...), used only to flag ambiguous rows for review.
PACK_RE = re.compile(r"\b\d+L?\s?X\s?\d+\s?(?:UN[DI]{0,2})?\b", re.I)

# The individual container size. This is
# a real product-identifying spec (a 1L bottle photo != a 4L bottle photo) so
# it must survive into nombre_limpio, unlike the case-count multiplier.
LITRAJE_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s?L(?=X|\b)", re.I)

# Case-count multiplier that follows a *confirmed* litraje marker, e.g. the
# "X12UN" in "1LX12UN" or "X4UND" in "4LX4UND". Only matches when preceded by
# <num>L, so ambiguous forms with no L (4X4 UNID, 3X5, 6X1) are left alone
# [get flagged via PACK_RE].
CASE_MULTIPLIER_RE = re.compile(
    r"\b(\d+(?:[.,]\d+)?\s?L)\s?X\s?\d+\s?(?:UN[DI]{0,2})?\b", re.I
)

# 10W30, 15W40, 5W40, 80W90, 20W50, 0W-16
VISCOSIDAD_RE = re.compile(r"\b\d{1,2}W-?\d{2}\b", re.I)

# 185/65R15
MEDIDA_LLANTA_RE = re.compile(r"\b\d{3}/\d{2}R\d{2}\b", re.I)

# Trailing 2-4 letter/digit platform code, optionally hyphenated compound
# (SA3-LN3, MG2-SC). Only mapped model names from vehicle_codes.py are, and that dict starts
# empty for those rows anyway.
VEHICULO_CODE_RE = re.compile(r"\b([A-Z]{1,4}\d{0,2}(?:-[A-Z]{1,4}\d{0,2})*)\s*$")
