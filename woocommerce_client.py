"""
WooCommerce REST + WordPress media client.

Upgraded features:
  - Connection pooling with automatic exponential backoff retry on 429/5xx via urllib3 Retry.
  - Image deduplication: computes MD5 hash and caches URL in Redis/memory to avoid re-uploading identical media.
  - Category caching: eliminates duplicate category lookup requests.
  - Cached SKU-to-ID catalog map (stored in Redis) to prevent hammering WooCommerce every 15 minutes during stock sync.
  - Order querying helpers with pagination and date filters for reconciliation.
"""

import base64
import io
import json
import logging
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from config import (
    WC_URL,
    WC_CONSUMER_KEY,
    WC_CONSUMER_SECRET,
    WP_USERNAME,
    WP_APP_PASSWORD,
    HTTP_TIMEOUT,
    IMAGE_TIMEOUT,
    DEBUG,
    STOCK_CACHE_TTL,
)
from utils import compute_md5

logger = logging.getLogger("woocommerce_client")

# Configure session with robust retry strategy
_session = requests.Session()
_retries = Retry(
    total=3,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    raise_on_status=False,
)
_adapter = HTTPAdapter(max_retries=_retries, pool_connections=10, pool_maxsize=20)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

# In-memory category cache
_category_cache: dict[str, dict] = {}
# In-memory image hash cache (fallback if Redis is not passed)
_image_hash_cache: dict[str, str] = {}


def wc_auth_params() -> dict:
    return {
        "consumer_key": WC_CONSUMER_KEY,
        "consumer_secret": WC_CONSUMER_SECRET,
    }


def _debug(label: str, response: requests.Response) -> None:
    if DEBUG:
        logger.debug("%s status=%s body=%s", label, response.status_code, response.text[:500])


def _request(method: str, path: str, params: dict | None = None, json_data: dict | None = None, timeout: tuple = HTTP_TIMEOUT):
    """Centralized request handler with auth, timeout, and response checks."""
    all_params = dict(params or {})
    all_params.update(wc_auth_params())

    url = f"{WC_URL}{path}"
    response = _session.request(
        method=method,
        url=url,
        params=all_params,
        json=json_data,
        timeout=timeout,
    )
    _debug(f"WooCommerce {method.upper()} {path}", response)

    if response.status_code == 429:
        retry_after = int(response.headers.get("Retry-After", 5))
        logger.warning("WooCommerce 429 Rate Limit. Sleeping %ds before single final retry...", retry_after)
        time.sleep(retry_after)
        response = _session.request(
            method=method,
            url=url,
            params=all_params,
            json=json_data,
            timeout=timeout,
        )

    response.raise_for_status()
    return response.json()


def wc_get(path: str, params: dict | None = None):
    return _request("GET", path, params=params)


def wc_post(path: str, payload: dict):
    return _request("POST", path, json_data=payload)


def wc_put(path: str, payload: dict):
    data = _request("PUT", path, json_data=payload)
    time.sleep(0.15)  # gentle pacing
    return data


def wc_delete(path: str, params: dict | None = None):
    return _request("DELETE", path, params=params)


# --------------------------------------------------------------------------
# Product & Category Lookups
# --------------------------------------------------------------------------
def find_product_by_sku(sku: str) -> dict | None:
    products = wc_get(
        "/wp-json/wc/v3/products",
        params={"sku": sku},
    )
    return products[0] if products else None


def find_category_by_name(category_name: str) -> dict | None:
    category_name_clean = category_name.strip()
    if category_name_clean.lower() in _category_cache:
        return _category_cache[category_name_clean.lower()]

    categories = wc_get(
        "/wp-json/wc/v3/products/categories",
        params={"search": category_name_clean, "per_page": 100},
    )
    for category in categories:
        if category["name"].strip().lower() == category_name_clean.lower():
            _category_cache[category_name_clean.lower()] = category
            return category
    return None


def create_category(category_name: str) -> dict:
    category_name_clean = category_name.strip()
    cat = wc_post(
        "/wp-json/wc/v3/products/categories",
        payload={"name": category_name_clean},
    )
    _category_cache[category_name_clean.lower()] = cat
    return cat


def get_or_create_category(category_name: str) -> dict:
    existing = find_category_by_name(category_name)
    if existing:
        return existing
    return create_category(category_name)


# --------------------------------------------------------------------------
# Image Upload with Deduplication
# --------------------------------------------------------------------------
def upload_product_image_from_base64(
    image_base64: str | None,
    sku: str,
    redis_client: Any | None = None,
) -> str | None:
    """
    Decode Base64 image and upload to WordPress Media library.

    Prevents duplicates:
      1. Hashes raw image bytes with MD5.
      2. Checks Redis/memory cache `wc:imghash:<sku>`. If hash matches, returns
         cached URL without uploading again.
      3. Closes buffer immediately in `finally` to prevent memory leaks.
    """
    if not image_base64:
        return None

    buffer: io.BytesIO | None = None
    try:
        raw_bytes = base64.b64decode(image_base64)
        image_base64 = None  # drop large string immediately

        img_hash = compute_md5(raw_bytes)
        cache_key = f"wc:imghash:{sku}"

        # 1. Check Redis or in-memory cache
        if redis_client:
            try:
                cached_data = redis_client.get(cache_key)
                if cached_data:
                    cached_obj = json.loads(cached_data) if isinstance(cached_data, str) else cached_data
                    if cached_obj.get("hash") == img_hash and cached_obj.get("url"):
                        logger.debug("Image for SKU %s unchanged; reusing URL: %s", sku, cached_obj["url"])
                        return cached_obj["url"]
            except Exception:
                logger.warning("Redis image hash lookup failed for sku=%s", sku)
        elif _image_hash_cache.get(sku) == img_hash:
            logger.debug("Image for SKU %s unchanged (in-memory match)", sku)
            return None  # No upload needed

        # 2. Upload to WordPress media
        buffer = io.BytesIO(raw_bytes)
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
            data=buffer,
            timeout=IMAGE_TIMEOUT,
        )
        _debug("WordPress MEDIA POST", response)
        response.raise_for_status()

        uploaded_url = response.json().get("source_url")

        # 3. Store hash in cache
        if uploaded_url:
            if redis_client:
                try:
                    redis_client.set(cache_key, json.dumps({"hash": img_hash, "url": uploaded_url}), ex=86400 * 30)
                except Exception:
                    pass
            _image_hash_cache[sku] = img_hash

        return uploaded_url

    except Exception:
        logger.exception("Image upload failed for sku=%s", sku)
        return None
    finally:
        if buffer is not None:
            buffer.close()


# --------------------------------------------------------------------------
# Stock-sync Helpers & SKU Map Caching
# --------------------------------------------------------------------------
def iter_wc_products_sku_map(per_page: int = 100) -> dict[str, int]:
    """
    Fetch all {sku: woo_product_id} mappings from WooCommerce across all pages.
    Pulls minimal fields: id, sku.
    """
    sku_to_id: dict[str, int] = {}
    page = 1
    while True:
        batch = wc_get(
            "/wp-json/wc/v3/products",
            params={
                "per_page": per_page,
                "page": page,
                "_fields": "id,sku",
            },
        )
        if not batch:
            break
        for prod in batch:
            sku = prod.get("sku")
            if sku:
                sku_to_id[sku.strip()] = prod["id"]
        if len(batch) < per_page:
            break
        page += 1
    return sku_to_id


def get_cached_wc_sku_map(redis_client: Any | None = None, force_refresh: bool = False) -> dict[str, int]:
    """
    Get SKU -> WooCommerce ID map, cached in Redis with STOCK_CACHE_TTL.
    Avoids fetching the full catalog every cycle.
    """
    cache_key = "wc:sku_to_id_map"
    if redis_client and not force_refresh:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                logger.info("Loaded WooCommerce SKU->ID map from Redis cache.")
                return json.loads(cached)
        except Exception:
            logger.warning("Redis error reading SKU map cache.")

    # Fresh load
    logger.info("Fetching fresh WooCommerce SKU->ID map from API...")
    mapping = iter_wc_products_sku_map()

    if redis_client and mapping:
        try:
            redis_client.set(cache_key, json.dumps(mapping), ex=STOCK_CACHE_TTL)
        except Exception:
            pass

    return mapping


def wc_batch_update_stock(updates: list[dict]):
    """Update stock for multiple products in a single batch call."""
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
    time.sleep(0.15)
    return response.json()


# --------------------------------------------------------------------------
# Order Querying & Status Helpers
# --------------------------------------------------------------------------
def get_wc_order(wc_order_id: int) -> dict:
    """Fetch order details for a given ID."""
    return wc_get(f"/wp-json/wc/v3/orders/{wc_order_id}")


def get_wc_orders(status: str | None = None, after: str | None = None, page: int = 1, per_page: int = 50) -> list[dict]:
    """Query orders with status and date filters (for reconciliation)."""
    params: dict[str, Any] = {"page": page, "per_page": per_page}
    if status:
        params["status"] = status
    if after:
        params["after"] = after
    return wc_get("/wp-json/wc/v3/orders", params=params)


def update_wc_order_status(wc_order_id: int, new_status: str, customer_note: str | None = None) -> dict:
    """Update WooCommerce order status (e.g. 'completed', 'cancelled')."""
    payload: dict[str, Any] = {"status": new_status}
    if customer_note:
        payload["customer_note"] = customer_note
    return wc_put(f"/wp-json/wc/v3/orders/{wc_order_id}", payload=payload)