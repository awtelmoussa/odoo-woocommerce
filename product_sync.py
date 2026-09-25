"""
Odoo -> WooCommerce product sync logic.

Resource-safety changes vs. the original:
  - The batch/list field set NO LONGER includes image_1920. Fetching 20 full
    records each carrying a multi-MB Base64 image was the main OOM driver.
    The image is fetched ONLY for the single product being synced, in its own
    read() call right before upload, so it is never held alongside category /
    SKU work or alongside other products' images.

Structure is kept modular:
  - field fetching       -> get_odoo_products / fetch_product_image_b64
  - payload mapping      -> build_product_payload
  - WooCommerce push     -> sync_product_record
"""

import logging

from odoo_client import OdooClient
from woocommerce_client import (
    find_product_by_sku,
    wc_post,
    wc_put,
    get_or_create_category,
    upload_product_image_from_base64,
)

logger = logging.getLogger("product_sync")

odoo = OdooClient()


# Fields safe to fetch in bulk -- NOTE: no image_1920 here on purpose.
PRODUCT_FIELDS = [
    "id",
    "name",
    "default_code",
    "list_price",
    "description_sale",
    "categ_id",
    "qty_available",
    "free_qty",
]


def get_odoo_products(limit: int = 20):
    """Bulk list without images (safe to hold many at once)."""
    return odoo.search_read("product.template", [], PRODUCT_FIELDS, limit=limit)


def fetch_product_image_b64(product_id: int) -> str | None:
    """
    Fetch image_1920 for a SINGLE product, isolated in its own read call so the
    large Base64 blob is loaded only at the moment it is needed.
    """
    records = odoo.read("product.template", [int(product_id)], ["image_1920"])
    if not records:
        return None
    return records[0].get("image_1920") or None


def build_product_payload(product: dict) -> dict | None:
    sku = product.get("default_code")
    if not sku:
        return None

    category = None
    if product.get("categ_id"):
        category_name = product["categ_id"][1]
        category = get_or_create_category(category_name)

    available_stock = int(max(0, product.get("free_qty") or 0))

    payload = {
        "sku": sku,
        "name": product["name"],
        "description": product.get("description_sale") or "",
        "regular_price": str(product.get("list_price") or 0),
        "manage_stock": True,
        "stock_quantity": available_stock,
        "stock_status": "instock" if available_stock > 0 else "outofstock",
        "type": "simple",
    }

    if category:
        payload["categories"] = [{"id": category["id"]}]

    # Fetch + upload the image in isolation, then forget it.
    image_b64 = fetch_product_image_b64(product["id"])
    if image_b64:
        image_url = upload_product_image_from_base64(image_b64, sku)
        image_b64 = None  # drop the big string reference
        if image_url:
            payload["images"] = [{"src": image_url}]

    return payload


def sync_product_record(product: dict):
    payload = build_product_payload(product)
    if not payload:
        logger.info("Skipping product without SKU: %s", product.get("name"))
        return

    existing = find_product_by_sku(payload["sku"])

    if existing:
        logger.info("Updating existing WooCommerce product for SKU %s", payload["sku"])
        updated = wc_put(f"/wp-json/wc/v3/products/{existing['id']}", payload)
        logger.info("Updated WooCommerce ID: %s", updated["id"])
        return updated

    logger.info("Creating WooCommerce product for SKU %s", payload["sku"])
    created = wc_post("/wp-json/wc/v3/products", payload)
    logger.info("Created WooCommerce ID: %s", created["id"])
    return created


def sync_products(limit: int = 20):
    products = get_odoo_products(limit=limit)
    logger.info("Found %d Odoo products", len(products))

    for product in products:
        logger.info("Processing: %s (Odoo ID %s, SKU %s)",
                    product.get("name"), product.get("id"), product.get("default_code"))
        try:
            sync_product_record(product)
        except Exception:
            logger.exception("Error processing product: %s", product.get("name"))
            continue


def sync_one_product_by_sku(sku: str):
    products = odoo.search_read(
        "product.template",
        [["default_code", "=", sku]],
        PRODUCT_FIELDS,
        limit=1,
    )
    if not products:
        logger.warning("Odoo product not found with SKU: %s", sku)
        return
    logger.info("Syncing Odoo product by SKU: %s", sku)
    return sync_product_record(products[0])


def sync_one_product_by_odoo_id(product_id):
    products = odoo.search_read(
        "product.template",
        [["id", "=", int(product_id)]],
        PRODUCT_FIELDS,
        limit=1,
    )
    if not products:
        logger.warning("Odoo product not found with ID: %s", product_id)
        return
    product = products[0]
    logger.info("Syncing Odoo product by ID %s (name=%s, sku=%s)",
                product_id, product.get("name"), product.get("default_code"))
    return sync_product_record(product)