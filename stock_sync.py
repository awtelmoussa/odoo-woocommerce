"""
Periodic Odoo -> WooCommerce STOCK sync (standalone runner).

Enhancements:
  - Eliminates the 1000-product cap: uses paged `search_read_all` to handle catalogs of any size.
  - Caches WooCommerce SKU->ID map in Redis to avoid querying the entire WooCommerce catalog every 15 minutes.
  - Fail-open Redis changed-only tracking: pushes only items whose stock has actually changed.
  - Batches writes to WooCommerce (100 per request) to prevent API rate limits.
  - Supports `--once` for cron jobs, `--refresh-cache` to force rebuild of the SKU map, and `--loop` for daemon mode.
"""

import argparse
import logging
import sys
import time
from typing import Any

import redis

from config import (
    REDIS_URL,
    STOCK_SYNC_INTERVAL_SECONDS,
    ODOO_LOCATION_ID,
    ODOO_WAREHOUSE_ID,
)
from odoo_client import OdooClient
from utils import sanitize_text, safe_int
from woocommerce_client import get_cached_wc_sku_map, wc_batch_update_stock

logger = logging.getLogger("stock_sync")

BATCH_SIZE = 100
LAST_STOCK_PREFIX = "stock:last:"
STOCK_FIELDS = ["id", "default_code", "free_qty", "qty_available"]

odoo = OdooClient(lazy=True)

# Synchronous Redis client for changed-only tracking
try:
    _redis = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
except Exception:
    _redis = None


def _chunked(items: list, size: int):
    """Yield successive `size`-length chunks from `items`."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _last_stock(sku: str) -> str | None:
    """Return last quantity pushed for this SKU, or None."""
    if not _redis:
        return None
    try:
        return _redis.get(f"{LAST_STOCK_PREFIX}{sku}")
    except Exception:
        return None


def _remember_stock(mapping: dict[str, int]) -> None:
    """Persist successfully pushed quantities in Redis."""
    if not mapping or not _redis:
        return
    try:
        pipe = _redis.pipeline()
        for sku, qty in mapping.items():
            pipe.set(f"{LAST_STOCK_PREFIX}{sku}", str(qty))
        pipe.execute()
    except Exception:
        logger.warning("Redis write failed while remembering stock values.")


def run_stock_sync_once(force_refresh_cache: bool = False, limit: int | None = None) -> int:
    """
    Execute one full stock synchronization cycle.
    Returns the number of products updated in WooCommerce.
    """
    logger.info("Stock sync cycle starting...")

    # 1. Fetch Odoo products with SKUs (full catalog pagination)
    domain = [["default_code", "!=", False], ["sale_ok", "=", True]]
    if limit:
        products = odoo.search_read("product.template", domain, STOCK_FIELDS, limit=limit)
    else:
        products = odoo.search_read_all("product.template", domain, STOCK_FIELDS, batch_size=200)

    logger.info("Fetched %d active products with SKUs from Odoo", len(products))
    if not products:
        logger.info("No Odoo products found for stock sync.")
        return 0

    # 2. Get WooCommerce SKU -> ID mapping (cached in Redis)
    sku_to_wc_id = get_cached_wc_sku_map(redis_client=_redis, force_refresh=force_refresh_cache)
    logger.info("Loaded %d SKU->ID mappings for WooCommerce", len(sku_to_wc_id))

    # 3. Diff against last-pushed values in Redis
    updates: list[dict[str, Any]] = []
    pushed_values: dict[str, int] = {}
    missing_in_woo = 0

    for p in products:
        sku = sanitize_text(p.get("default_code"))
        if not sku:
            continue

        wc_id = sku_to_wc_id.get(sku)
        if not wc_id:
            missing_in_woo += 1
            continue

        qty = safe_int(max(0, p.get("free_qty") or 0))

        # Check if unchanged since last push
        if _last_stock(sku) == str(qty):
            continue

        updates.append({
            "id": wc_id,
            "stock_quantity": qty,
            "stock_status": "instock" if qty > 0 else "outofstock",
            "manage_stock": True,
        })
        pushed_values[sku] = qty

    if missing_in_woo:
        logger.debug("%d Odoo products have no matching SKU in WooCommerce", missing_in_woo)

    if not updates:
        logger.info("No stock changes detected this cycle. 0 WooCommerce writes.")
        return 0

    # 4. Push updates to WooCommerce in batches
    logger.info("Pushing %d stock updates to WooCommerce in batches of %d...", len(updates), BATCH_SIZE)
    sent_ok: dict[str, int] = {}

    for chunk in _chunked(updates, BATCH_SIZE):
        try:
            wc_batch_update_stock(chunk)
            chunk_ids = {u["id"] for u in chunk}
            for sku, qty in pushed_values.items():
                if sku_to_wc_id.get(sku) in chunk_ids:
                    sent_ok[sku] = qty
        except Exception:
            logger.exception("Batch stock update failed for chunk of %d products", len(chunk))

    # 5. Remember pushed quantities
    _remember_stock(sent_ok)
    logger.info("Stock sync cycle completed: %d products updated in WooCommerce.", len(sent_ok))
    return len(sent_ok)


def run_loop() -> None:
    """Run forever on the configured interval."""
    logger.info("Stock sync loop started (interval=%ds).", STOCK_SYNC_INTERVAL_SECONDS)
    while True:
        try:
            run_stock_sync_once()
        except Exception:
            logger.exception("Stock sync cycle encountered an error; will retry next interval.")
        time.sleep(STOCK_SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Periodic Odoo -> WooCommerce stock sync")
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single stock sync cycle and exit (useful for cron).",
    )
    parser.add_argument(
        "--refresh-cache", action="store_true",
        help="Force fresh lookup of all WooCommerce product SKUs.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Optional limit on Odoo products to inspect.",
    )
    args = parser.parse_args()

    if args.once:
        run_stock_sync_once(force_refresh_cache=args.refresh_cache, limit=args.limit)
        sys.exit(0)

    run_loop()