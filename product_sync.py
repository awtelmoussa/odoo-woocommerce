"""
Odoo -> WooCommerce product sync logic.

Upgrades:
  - Lazy OdooClient initialization (no crash on import).
  - Flexible `sync_one_product` entrypoint supporting either SKU or Odoo ID.
  - Distinguishes storable ('product') vs service/consumable ('service', 'consu') so service items don't have stock managed.
  - String and HTML sanitization for XML-RPC and database cleanliness.
  - Image deduplication: passes Redis client to prevent uploading duplicate media to WordPress.
  - Category resolution with automatic caching.
"""

import logging
from typing import Any

import redis

from config import REDIS_URL
from odoo_client import OdooClient
from utils import sanitize_text, safe_float, safe_int
from woocommerce_client import (
    find_product_by_sku,
    wc_post,
    wc_put,
    get_or_create_category,
    upload_product_image_from_base64,
)

logger = logging.getLogger("product_sync")

# Lazy Odoo client
odoo = OdooClient(lazy=True)

# Synchronous Redis client for image hash caching
try:
    _redis = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )
except Exception:
    _redis = None

# Fields safe to fetch in bulk (image_1920 fetched lazily on demand)
PRODUCT_FIELDS = [
    "id",
    "name",
    "default_code",
    "list_price",
    "description_sale",
    "categ_id",
    "type",
    "qty_available",
    "free_qty",
    "active",
]


def get_odoo_products(limit: int = 50, offset: int = 0) -> list[dict]:
    """Bulk list without heavy image fields."""
    return odoo.search_read(
        "product.template",
        domain=[["sale_ok", "=", True]],
        fields=PRODUCT_FIELDS,
        limit=limit,
        offset=offset,
    )


def fetch_product_image_b64(product_id: int) -> str | None:
    """Fetch image_1920 for a single product isolated in its own query."""
    try:
        records = odoo.read("product.template", [int(product_id)], ["image_1920"])
        if records and records[0].get("image_1920"):
            return records[0]["image_1920"]
    except Exception:
        logger.warning("Could not read image for product %s", product_id)
    return None


def build_product_payload(product: dict) -> dict | None:
    sku = sanitize_text(product.get("default_code"))
    if not sku:
        return None

    name = sanitize_text(product.get("name"))
    description = sanitize_text(product.get("description_sale") or "")
    price = safe_float(product.get("list_price"))
    prod_type = product.get("type", "product")  # 'product' (storable), 'consu', 'service'

    category = None
    if product.get("categ_id") and isinstance(product["categ_id"], (list, tuple)):
        category_name = sanitize_text(product["categ_id"][1])
        if category_name:
            category = get_or_create_category(category_name)

    # Determine stock management: only storable products ('product') manage inventory
    if prod_type == "product":
        available_stock = safe_int(max(0, product.get("free_qty") or 0))
        manage_stock = True
        stock_status = "instock" if available_stock > 0 else "outofstock"
    else:
        available_stock = None
        manage_stock = False
        stock_status = "instock"

    payload: dict[str, Any] = {
        "sku": sku,
        "name": name,
        "description": description,
        "regular_price": str(price),
        "manage_stock": manage_stock,
        "stock_status": stock_status,
        "type": "simple",
    }

    if manage_stock and available_stock is not None:
        payload["stock_quantity"] = available_stock

    if category:
        payload["categories"] = [{"id": category["id"]}]

    # Fetch and upload image if present, leveraging deduplication cache
    image_b64 = fetch_product_image_b64(product["id"])
    if image_b64:
        image_url = upload_product_image_from_base64(image_b64, sku, redis_client=_redis)
        image_b64 = None  # drop reference immediately
        if image_url:
            payload["images"] = [{"src": image_url}]

    return payload


def sync_product_record(product: dict) -> dict | None:
    """Sync a single Odoo product dictionary to WooCommerce."""
    payload = build_product_payload(product)
    if not payload:
        logger.info("Skipping product without SKU: %s (id=%s)", product.get("name"), product.get("id"))
        return None

    sku = payload["sku"]
    existing = find_product_by_sku(sku)

    if existing:
        logger.info("Updating existing WooCommerce product id=%s for SKU %s", existing["id"], sku)
        updated = wc_put(f"/wp-json/wc/v3/products/{existing['id']}", payload)
        return updated

    logger.info("Creating new WooCommerce product for SKU %s", sku)
    created = wc_post("/wp-json/wc/v3/products", payload)
    return created


def sync_one_product_by_sku(sku: str) -> dict | None:
    sku = sanitize_text(sku)
    products = odoo.search_read(
        "product.template",
        [["default_code", "=", sku]],
        PRODUCT_FIELDS,
        limit=1,
    )
    if not products:
        logger.warning("Odoo product not found with SKU: %s", sku)
        return None
    return sync_product_record(products[0])


def sync_one_product_by_odoo_id(product_id: int) -> dict | None:
    products = odoo.search_read(
        "product.template",
        [["id", "=", int(product_id)]],
        PRODUCT_FIELDS,
        limit=1,
    )
    if not products:
        logger.warning("Odoo product not found with ID: %s", product_id)
        return None
    return sync_product_record(products[0])


def sync_one_product(identifier: str | int) -> dict | None:
    """
    Convenience entrypoint accepting either an Odoo record ID or a SKU string.
    """
    if isinstance(identifier, int) or (isinstance(identifier, str) and identifier.isdigit()):
        return sync_one_product_by_odoo_id(int(identifier))
    return sync_one_product_by_sku(str(identifier))


def sync_products(limit: int = 50, offset: int = 0) -> int:
    """Bulk sync products from Odoo to WooCommerce."""
    products = get_odoo_products(limit=limit, offset=offset)
    logger.info("Found %d Odoo products to sync (offset=%d, limit=%d)", len(products), offset, limit)

    synced_count = 0
    for product in products:
        try:
            res = sync_product_record(product)
            if res:
                synced_count += 1
        except Exception:
            logger.exception("Failed to sync product: %s (id=%s)", product.get("name"), product.get("id"))

    logger.info("Finished product sync: %d/%d synced successfully.", synced_count, len(products))
    return synced_count