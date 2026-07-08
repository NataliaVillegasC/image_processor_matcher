"""Image search via DuckDuckGo (ddgs library). No API key needed."""
from ddgs import DDGS

import config


def search_images(query: str, max_results: int = config.MAX_SEARCH_RESULTS) -> list[dict]:
    """Run an image search and return a list of clean candidate dicts.

    Each candidate has:
      image_url  - direct URL of the full-size image (what we download)
      page_url   - the web page the image lives on (future verifying of the source)
      title      - page/image title
      width/height - dimensions as reported by the search engine
      source_site  - domain hosting the image
    """
    with DDGS() as ddgs:
        raw = ddgs.images(
            query,
            max_results=max_results,
            size="Large",          # pre-filters low resolution for us
            type_image="photo",    # no clipart/drawings
            safesearch="moderate",
        )

    candidates = []
    for item in raw:
        candidates.append({
            "image_url": item.get("image"),
            "thumbnail_url": item.get("thumbnail"),  # small preview to display
            "page_url": item.get("url"),
            "title": item.get("title", ""),
            "width": item.get("width", 0),
            "height": item.get("height", 0),
            "source_site": item.get("source", ""),
        })
    return candidates


if __name__ == "__main__":
    from read_excel import load_products
    from query_builder import build_query

    products = load_products()
    row = products.iloc[0]  # first product: MOTRIO oil 8550504748
    query = build_query(row)
    print(f"Query: {query}\n")

    results = search_images(query)
    print(f"Got {len(results)} candidates:\n")
    for i, c in enumerate(results, 1):
        print(f"{i:2}. {c['width']}x{c['height']}  {c['title'][:60]}")
        print(f"    page:  {c['page_url'][:90]}")
        print(f"    image: {c['image_url'][:90]}\n")