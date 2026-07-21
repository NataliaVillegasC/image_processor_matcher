# Product Image Pipeline (DONREP)

This tool takes a catalog of products (an Excel file) and, for each product,
finds the best product image on the web and writes it back into a new Excel file
with thumbnails you can look at.

It does this in 6 steps automatically:

1. **Normalize** -> clean up each product name.
2. **Categorize** -> figure out what kind of product it is.
3. **Search** -> search Google for candidate images.
4. **Pre-filter** -> throw out bad candidates (too small, duplicates, banners...).
5. **Select** -> ask Google's Gemini AI to pick the single best image.
6. **Export** -> download the chosen image and build the final Excel file.

To run it do the command: `python run_pipeline.py`.

---

## What you need before running

1. **Python 3.11** installed.
2. Two secret files in this folder. Each covers a different part of the pipeline:
   - **`.env`** - the **Google Custom Search (CSE)** credentials for the image
     _search_ step: an API key (`GOOGLE_CSE_API_KEYS`) and the search-engine id
     (`GOOGLE_CSE_CX`). It also holds the Vertex AI project/region settings.
     Template: [`.env.example`](.env.example).
   - **`credentials.json`** - the **Google Vertex AI service-account key** for the
     _Gemini_ selection step (the AI that picks the best image).
     Template: [`credentials.json.example`](credentials.json.example).
3. The **input catalog** at `input/products.xlsx`. It must have these columns:
   `Ref Proveedor`, `Marca`, `Nombre`.

> **Where the values come from** (Google Cloud console, same project):
>
> - _CSE API key_ → APIs & Services → Credentials → create an API key.
> - _CSE search-engine id (`cx`)_ → programmablesearchengine.google.com → your
>   engine → "Search engine ID".
> - _`credentials.json`_ → IAM & Admin → Service Accounts → your account → Keys →
>   "Add key" → JSON. Download it and save it here as `credentials.json`.

---

## Setup

Open a terminal in this folder and run:

```bash
# 1. Create and activate a virtual environment
python3.11 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate

# 2. Install the libraries the pipeline needs
pip install -r requirements.txt

# 3. Create your .env from the template, then edit it with the prod values
cp .env.example .env
#   ...open .env in an editor and paste the CSE key + cx + project settings...

# 4. Create credentials.json for the Gemini step. Easiest: download the
#    service-account JSON key from Google Cloud and save it here as
#    credentials.json. (credentials.json.example shows the expected shape.)
```

---

## Running the pipeline

Always activate the environment first (`source venv/bin/activate`), then:

```bash
# Recommended: Quick test on 5 products first to confirm credentials work
python run_pipeline.py --limit 5

# Then run the full catalog
python run_pipeline.py
```

While it runs it prints a timestamped line for each step to follow along.
When it finishes it prints a short summary (how many products got an image) and
the location of the final file.

### Where the results go

- **Final Excel:** `output_prod/catalogo_final.xlsx` -> your original catalog plus
  new columns (`imagen_url`, `imagen_local`, `categoria`, `confianza`, `flags`,
  `razon_gemini`, ...) and a `miniatura` column with the actual image thumbnail.
- **Downloaded images:** `output_prod/images/`
- **Cache:** `cache_prod/` - saved API responses (explained below).

---

## Important: the daily search limit

Google's **free** Custom Search tier only allows **100 new searches per day**.
The catalog has 281 products, so a first full run **cannot finish in one day** on
the free tier, it will do ~100 products and stop searching for the rest.

**This is fine and expected.** The pipeline **caches** everything it already did in
`cache_prod/`. So you just run the same command again the next day and it will:

- **skip** everything already done (no cost, instant), and
- **only** search the products that are still missing.

Repeat across a few days until all 281 are done.

> If production uses a **paid** Custom Search plan with a higher limit, you can
> finish in one run (no special flags needed).

To deliberately stop after N new searches (e.g. to stay under a budget):

```bash
python run_pipeline.py --max-queries 90
```

---

## Common options

| Option            | What it does                                                 |
| ----------------- | ------------------------------------------------------------ |
| `--limit N`       | Only process the first N products (great for a quick test).  |
| `--max-queries N` | Stop after N **new** Google searches this run.               |
| `--max-calls N`   | Stop after N **new** Gemini AI calls this run.               |
| `--input PATH`    | Use a different input Excel (default `input/products.xlsx`). |
| `--output-dir P`  | Change where results are written (default `output_prod/`).   |
| `--cache-dir P`   | Change where the cache lives (default `cache_prod/`).        |
| `--help`          | Show all options.                                            |

---

## Troubleshooting

- **`ERROR: no .env file found`** → run `cp .env.example .env` and fill it in.
- **`ERROR: these variables are missing in .env`** → open `.env` and make sure
  every line has a value after the `=`.
- **`ERROR: Google service-account key not found`** → put the production
  `credentials.json` in this folder (or point `GOOGLE_APPLICATION_CREDENTIALS`
  in `.env` at its location).
- **`ERROR: input Excel not found`** → put the catalog at `input/products.xlsx`
  or pass `--input path/to/your.xlsx`.
- **Searches suddenly stop / many products get no image** → you probably hit the
  100-searches-per-day free limit. Wait until tomorrow and run the same command
  again; it resumes automatically from the cache.
- **Want to start completely fresh?** → delete the `cache_prod/` and
  `output_prod/` folders, then run again (this re-pays for all API calls).

---

### Example of a run with all the flags:

```bash
python run_pipeline.py --limit 5 --max-queries 5 --max-calls 5
```

- --limit 5 → only the first 5 products
- --max-queries 5 → at most 5 new CSE searches this run (guards your 100/day quota)
- --max-calls 5 → at most 5 new Gemini calls (guards cost)
