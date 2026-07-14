"""Load products from the input Excel and clean them for the pipeline."""
import pandas as pd

import config


def load_products(n_rows: int = config.N_ROWS) -> pd.DataFrame:
    """Return the first n_rows products with clean ref/brand/name columns.

    Adds a `product_id` column (the Ref Proveedor) used to name image files
    and to join candidates.csv / decisions.csv later.
    """
    df = pd.read_excel(config.INPUT_XLSX, dtype={config.COL_REF: str})

    # Keep only the columns the pipeline needs
    df = df[[config.COL_REF, config.COL_BRAND, config.COL_NAME]].copy()

    # Clean whitespace and drop rows missing a reference (our search key)
    for col in df.columns:
        df[col] = df[col].astype(str).str.strip()
    df = df[df[config.COL_REF].notna() & (df[config.COL_REF] != "") & (df[config.COL_REF] != "nan")]

    df = df.head(n_rows).reset_index(drop=True)
    df["product_id"] = df[config.COL_REF]
    return df


if __name__ == "__main__":
    products = load_products()
    print(f"Loaded {len(products)} products:\n")
    print(products.to_string())
