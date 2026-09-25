"""
WooCommerce REST + WordPress media client.

Resource-safety changes vs. the original:
  1. EVERY request now has an explicit timeout. The original calls used the
     `requests` default, which is None (wait forever). One slow response would
     hang a worker thread permanently; under load every thread hangs and the
     server dies silently. This is fixed here.
  2. The image upload decodes Base64 into an io.BytesIO buffer, drops the big
     string reference immediately, and closes the buffer in `finally` so the
     decoded bytes do not linger in RAM. Prevents OOM under concurrent load.
  3. A single shared `requests.Session` reuses TCP connections instead of
     opening a fresh socket per call.

Note on time.sleep():
  wc_put still uses time.sleep() for rate-limit backoff and pacing. That is
  acceptable ONLY because this module runs inside a background threadpool, not
  on the asyncio event loop. If you ever call these functions from an async
  context directly, switch to asyncio.sleep.
"""

import base64
import io
import logging
import time

import requests

from config import (
    WC_URL,
    WC_CONSUMER_KEY,
    WC_CONSUMER_SECRET,
    WP_USERNAME,
    WP_APP_PASSWORD,
    HTTP_TIMEOUT,
    IMAGE_TIMEOUT,
    DEBUG,
)

logger = logging.getLogger("woocommerce_client")

# Shared session -> connection pooling / keep-alive across calls.
_session = requests.Session()


def wc_auth_params() -> dict:
    return {
        "consumer_key": WC_CONSUMER_KEY,
        "consumer_secret": WC_CONSUMER_SECRET,
    }


def _debug(label: str, response: requests.Response) -> None:
    if DEBUG:
        logger.debug("%s status=%s body=%s", label, response.status_code, response.text)


def wc_get(path: str, params: dict | None = None):
    params = dict(params or {})
    params.update(wc_auth_params())

    response = _session.get(
        f"{WC_URL}{path}",
        params=params,
        timeout=HTTP_TIMEOUT,
    )
    _debug("WooCommerce GET", response)
    response.raise_for_status()
    return response.json()


def wc_post(path: str, payload: dict):
    response = _session.post(
        f"{WC_URL}{path}",
        params=wc_auth_params(),
        json=payload,
        timeout=HTTP_TIMEOUT,
    )
    _debug("WooCommerce POST", response)
    response.raise_for_status()
    return response.json()


def wc_put(path: str, payload: dict):
    response = _session.put(
        f"{WC_URL}{path}",
        params=wc_auth_params(),
        json=payload,
        timeout=HTTP_TIMEOUT,
    )

    # Simple single-retry on rate limit.
    if response.status_code == 429:
        logger.warning("WooCommerce rate limit (429). Retrying in 10s...")
        time.sleep(10)  # OK: background threadpool, not the event loop.
        response = _session.put(
            f"{WC_URL}{path}",
            params=wc_auth_params(),
            json=payload,
            timeout=HTTP_TIMEOUT,
        )

    _debug("WooCommerce PUT", response)
    response.raise_for_status()

    # Gentle pacing so a burst of edits does not hammer the API.
    time.sleep(0.3)  # OK: background threadpool, not the event loop.
    return response.json()


def find_product_by_sku(sku: str):
    products = wc_get(
        "/wp-json/wc/v3/products",
        params={"sku": sku},
    )
    return products[0] if products else None


def find_category_by_name(category_name: str):
    categories = wc_get(
        "/wp-json/wc/v3/products/categories",
        params={"search": category_name, "per_page": 100},
    )
    for category in categories:
        if category["name"].lower() == category_name.lower():
            return category
    return None


def create_category(category_name: str):
    response = _session.post(
        f"{WC_URL}/wp-json/wc/v3/products/categories",
        params=wc_auth_params(),
        json={"name": category_name},
        timeout=HTTP_TIMEOUT,
    )
    _debug("WooCommerce CATEGORY POST", response)
    response.raise_for_status()
    return response.json()


def get_or_create_category(category_name: str):
    existing = find_category_by_name(category_name)
    if existing:
        return existing
    return create_category(category_name)


def upload_product_image_from_base64(image_base64: str | None, sku: str) -> str | None:
    """
    Decode a Base64 image and upload it to the WordPress media library.

    Memory-safe: decodes once into an io.BytesIO buffer, drops the source
    string reference, and closes the buffer in `finally`. Returns the public
    media source_url, or None on missing input / failure.
    """
    if not image_base64:
        return None

    buffer: io.BytesIO | None = None
    try:
        # Decode straight into a buffer; then release the big base64 string.
        buffer = io.BytesIO(base64.b64decode(image_base64))
        image_base64 = None  # drop the large string reference ASAP

        filename = f"{sku}.jpg"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": "image/jpeg",
        }

        buffer.seek(0)
        response = _session.post(
            f"{WC_URL}/wp-json/wp/v2/media",
            auth=(WP_USERNAME, WP_APP_PASSWORD),
            headers=headers,
            data=buffer,            # streamed from the buffer, not a raw bytes copy
            timeout=IMAGE_TIMEOUT,  # longer read window for large uploads
        )
        _debug("WordPress MEDIA POST", response)
        response.raise_for_status()

        return response.json().get("source_url")

    except Exception:
        logger.exception("Image upload failed for sku=%s", sku)
        return None

    finally:
        if buffer is not None:
            buffer.close()


# --------------------------------------------------------------------------
# Stock-sync helpers (used by stock_sync.py)
# --------------------------------------------------------------------------
def iter_wc_products_sku_map(per_page: int = 100) -> dict[str, int]:
    """
    Build a {sku: woo_product_id} map for the whole catalog, paging through the
    products endpoint. Requests only id+sku (_fields=) so each page is small.

    Cheap on RAM: only two fields per product are pulled, and we keep just the
    map (sku -> id), not the product bodies.
    """
    sku_to_id: dict[str, int] = {}
    page = 1
    while True:
        batch = wc_get(
            "/wp-json/wc/v3/products",
            params={
                "per_page": per_page,
                "page": page,
                "_fields": "id,sku",   # minimal payload
            },
        )
        if not batch:
            break
        for prod in batch:
            sku = prod.get("sku")
            if sku:
                sku_to_id[sku] = prod["id"]
        if len(batch) < per_page:
            break  # last page
        page += 1
    return sku_to_id


def wc_batch_update_stock(updates: list[dict]):
    """
    Update stock for many products in one call via the WooCommerce batch
    endpoint. `updates` items look like:
        {"id": 62645, "stock_quantity": 30,
         "stock_status": "instock", "manage_stock": True}

    WooCommerce caps batch ops (commonly ~100 items), so callers should chunk.
    Returns the parsed response, or None if there was nothing to send.
    """
    if not updates:
        return None
    response = _session.post(
        f"{WC_URL}/wp-json/wc/v3/products/batch",
        params=wc_auth_params(),
        json={"update": updates},
        timeout=HTTP_TIMEOUT,
    )
    _debug("WooCommerce BATCH stock update", response)
    response.raise_for_status()
    time.sleep(0.3)  # gentle pacing
    return response.json()