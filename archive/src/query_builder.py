"""Turn a product row into search query strings for image search."""
import pandas as pd

import config


def build_query(row: pd.Series) -> str:
    """Primary query: exact part number + brand.

    The Ref Proveedor is the most precise identifier we have, we need to use it
    for our best try of getting the correct brand's product
    """
    ref = row[config.COL_REF]
    brand = row[config.COL_BRAND]
    return f'"{ref}" "{brand}"'


def build_fallback_query(row: pd.Series) -> str:
    """Fallback when the ref returns nothing: brand + product name.

    Less precise so is preferable second option if the primary query
    gave little results.
    """
    ref = row[config.COL_REF]
    brand = row[config.COL_BRAND]
    name = row[config.COL_NAME]
    return f'"{brand}" {name} product with reference "{ref}" white background'


if __name__ == "__main__":
    from read_excel import load_products

    products = load_products()
    for _, row in products.iterrows():
        print(f"primary : {build_query(row)}")
        print(f"fallback: {build_fallback_query(row)}\n")
