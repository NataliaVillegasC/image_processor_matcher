"""Central configuration for the image matcher pipeline."""
from pathlib import Path

# ---------- Paths ----------
ROOT = Path(__file__).resolve().parent.parent

INPUT_XLSX = ROOT / "input" / "products.xlsx"
OUTPUT_XLSX = ROOT / "output" / "products_with_images.xlsx"

DATA_DIR = ROOT / "data"
IMAGES_DIR = DATA_DIR / "images"
CANDIDATES_CSV = DATA_DIR / "candidates.csv"
DECISIONS_CSV = DATA_DIR / "decisions.csv"

# ---------- Excel columns ----------
COL_REF = "Ref Proveedor"
COL_BRAND = "Marca"
COL_NAME = "Nombre"

# ---------- Pipeline (MVP) ----------
N_ROWS = 10                # only process the first N products
MAX_SEARCH_RESULTS = 20    # images to request from DDGS per product
MAX_DOWNLOADS = 8          # top candidates to actually download per product

# ---------- Image checks (SIMPLE FILTER) ----------
MIN_WIDTH = 500            # reject images smaller than this
MIN_HEIGHT = 500
BORDER_STD_MAX = 25.0      # max std-dev of border pixels to call bg "uniform"
BORDER_SAMPLE_PX = 10      # how thick a border strip to sample

# ---------- Download ----------
REQUEST_TIMEOUT = 10       # seconds per image download
# Identify our downloads as a normal browser so image hosts don't block us
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
