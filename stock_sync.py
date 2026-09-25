"""
Periodic Odoo -> WooCommerce STOCK sync (standalone runner).

Why this exists:
  Stock in Odoo lives in stock.quant, not on product.template. Inventory
  adjustments, purchase receipts, and sale fulfillment change stock WITHOUT
  editing the product record, so the product webhook never fires for them.
  This job is the reliable, self-healing baseline: every cycle it reads current
  stock from Odoo and pushes it to WooCommerce, regardless of HOW it changed.

Resource profile (intentionally light):
  - Reads ONLY [id, default_code, free_qty] from Odoo -- no image_1920, so the
    payload is tiny and there is no OOM risk.
  - "Changed-only": remembers the last quantity pushed per SKU in Redis and
    sends ONLY products whose stock actually changed. Idle cycles do zero
    WooCommerce writes.
  - Updates WooCommerce in BATCHES (one call per ~100 products) instead of two
    calls per product.
  - Sleeps between cycles (default 15 min) -> ~zero CPU when idle.

Redis note:
  This is plain synchronous code (no event loop), so it uses its OWN synchronous
  Redis client -- separate from cooldown.py which is async (used by the FastAPI
  app). They talk to the same Redis server, just different key prefixes.

Run:
  python stock_sync.py            # loops forever on the configured interval
  python stock_sync.py --once     # run a single cycle and exit (good for cron)
"""

import argparse
import logging
import sys
import time

import redis

from config import (
    REDIS_URL,
    STOCK_SYNC_INTERVAL_SECONDS,
    STOCK_SYNC_LIMIT,
)
from odoo_client import OdooClient
from woocommerce_client import iter_wc_products_sku_map, wc_batch_update_stock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("stock_sync")

# WooCommerce batch endpoint cap (keep a margin below the usual 100 limit).
BATCH_SIZE = 100

# Redis key prefix for "last stock value pushed" per SKU.
LAST_STOCK_PREFIX = "stock:last:"

# Only id + SKU + stock -- deliberately NO image_1920.
STOCK_FIELDS = ["id", "default_code", "free_qty"]


# --------------------------------------------------------------------------
# Clients (built once at module load)
# --------------------------------------------------------------------------
odoo = OdooClient()

# Synchronous Redis client for the changed-only tracking. Short timeouts so a
# Redis hiccup never hangs the loop.
_redis = redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=2.0,
    socket_timeout=2.0,
)


def _chunked(items: list, size: int):
    """Yield successive `size`-length chunks from `items`."""
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _last_stock(sku: str) -> str | None:
    """Last quantity we pushed for this SKU (as str), or None. Fail-open."""
    try:
        return _redis.get(f"{LAST_STOCK_PREFIX}{sku}")
    except Exception:
        logger.exception("Redis read failed for sku=%s; treating as changed", sku)
        return None  # treat as 'unknown' -> will push (safe direction)


def _remember_stock(mapping: dict[str, int]) -> None:
    """Store the quantities we just pushed, so next cycle can diff. Fail-open."""
    if not mapping:
        return
    try:
        pipe = _redis.pipeline()
        for sku, qty in mapping.items():
            pipe.set(f"{LAST_STOCK_PREFIX}{sku}", str(qty))
        pipe.execute()
    except Exception:
        logger.exception("Redis write failed while remembering stock values")


def run_stock_sync_once() -> None:
    """
    One full cycle:
      1. Pull [id, sku, free_qty] from Odoo.
      2. Map Odoo SKUs -> WooCommerce product IDs.
      3. Diff against last-pushed values (Redis); keep only changed.
      4. Push changed stock to WooCommerce in batches.
      5. Remember the new values.
    """
    logger.info("Stock sync cycle starting...")

    # 1. Odoo stock snapshot (image-free, cheap).
    products = odoo.search_read(
        "product.template",
        [["default_code", "!=", False]],   # only products that have a SKU
        STOCK_FIELDS,
        limit=STOCK_SYNC_LIMIT,
    )
    logger.info("Fetched %d products with SKUs from Odoo", len(products))

    if not products:
        logger.info("Nothing to sync.")
        return

    # 2. WooCommerce SKU -> ID map (one paged lookup, two fields each).
    sku_to_wc_id = iter_wc_products_sku_map()
    logger.info("Loaded %d SKU->ID mappings from WooCommerce", len(sku_to_wc_id))

    # 3. Build the changed-only update list.
    updates: list[dict] = []
    pushed_values: dict[str, int] = {}
    missing_in_woo = 0

    for p in products:
        sku = p.get("default_code")
        if not sku:
            continue

        wc_id = sku_to_wc_id.get(sku)
        if not wc_id:
            missing_in_woo += 1
            continue  # product not in Woo yet; product sync handles creation

        qty = int(max(0, p.get("free_qty") or 0))

        # Skip if unchanged since last push.
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
        logger.info("%d Odoo products have no matching WooCommerce SKU (skipped)",
                    missing_in_woo)

    if not updates:
        logger.info("No stock changes this cycle. 0 WooCommerce writes.")
        return

    # 4. Push in batches.
    logger.info("Pushing %d changed stock values to WooCommerce...", len(updates))
    sent_ok: dict[str, int] = {}
    for chunk in _chunked(updates, BATCH_SIZE):
        try:
            wc_batch_update_stock(chunk)
            # Mark this chunk's SKUs as successfully pushed.
            chunk_ids = {u["id"] for u in chunk}
            for sku, qty in pushed_values.items():
                if sku_to_wc_id.get(sku) in chunk_ids:
                    sent_ok[sku] = qty
        except Exception:
            logger.exception("Batch stock update failed for a chunk of %d", len(chunk))
            # Do NOT remember failed values -> they retry next cycle (self-healing).

    # 5. Remember only what actually went through.
    _remember_stock(sent_ok)
    logger.info("Stock sync cycle done. %d products updated.", len(sent_ok))


def run_loop() -> None:
    """Run forever on the configured interval, surviving per-cycle errors."""
    logger.info("Stock sync loop started (interval=%ss).", STOCK_SYNC_INTERVAL_SECONDS)
    while True:
        try:
            run_stock_sync_once()
        except Exception:
            logger.exception("Stock sync cycle crashed; will retry next interval")
        time.sleep(STOCK_SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Periodic Odoo->Woo stock sync")
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single cycle and exit (for cron). Default is loop forever.",
    )
    args = parser.parse_args()

    if args.once:
        run_stock_sync_once()
        sys.exit(0)

    run_loop()