"""
WooCommerce order -> Odoo Sales Order import.

Behavior (per chosen safe defaults):
  - Customer: matched by email; created as a new res.partner if not found.
  - Order state: created as a DRAFT QUOTATION (sale.order in 'draft'). Nothing
    is auto-confirmed, so no stock is moved automatically -- a human reviews and
    confirms in Odoo. This avoids double-decrementing stock (Woo already
    decremented on purchase).
  - Unknown SKU: if ANY line item's SKU is not found in Odoo, the WHOLE order is
    rejected (nothing is created) and the reason is logged. Prevents importing a
    partial/incorrect order whose total would not match.

Idempotency:
  Woo may deliver the same order webhook more than once (retries). We write the
  Woo order id into the SO's `client_order_ref` and check for an existing SO
  with that ref BEFORE creating, so a redelivery never creates a duplicate.

Resource safety:
  All Odoo calls go through OdooClient (timeout transport + logging). All Woo
  calls go through woocommerce_client (enforced timeouts). This module is
  synchronous and is intended to run inside the FastAPI background threadpool.
"""

import logging

from odoo_client import OdooClient
from woocommerce_client import wc_get

logger = logging.getLogger("order_sync")

odoo = OdooClient()


class UnknownSkuError(Exception):
    """Raised when a Woo line item references a SKU not present in Odoo."""


def _fetch_wc_order(wc_order_id: int) -> dict:
    """Fetch the full order from WooCommerce (timeout-bounded)."""
    return wc_get(f"/wp-json/wc/v3/orders/{wc_order_id}")


def _existing_so_id(wc_order_id: int) -> int | None:
    """Return the Odoo sale.order id already linked to this Woo order, if any."""
    ref = f"WOO-{wc_order_id}"
    ids = odoo.search("sale.order", [["client_order_ref", "=", ref]], limit=1)
    return ids[0] if ids else None


def _resolve_partner(order: dict) -> int:
    """
    Find or create the res.partner for this order's customer, matched by email.
    """
    billing = order.get("billing") or {}
    email = (billing.get("email") or "").strip().lower()

    name = " ".join(
        part for part in [billing.get("first_name"), billing.get("last_name")] if part
    ).strip() or billing.get("company") or "Online Customer"

    if email:
        existing = odoo.search("res.partner", [["email", "=", email]], limit=1)
        if existing:
            return existing[0]

    # Create a new partner with the billing details we have.
    values = {
        "name": name,
        "email": email or False,
        "phone": billing.get("phone") or False,
        "street": billing.get("address_1") or False,
        "street2": billing.get("address_2") or False,
        "city": billing.get("city") or False,
        "zip": billing.get("postcode") or False,
        "customer_rank": 1,  # mark as a customer
    }
    partner_id = odoo.create("res.partner", values)
    logger.info("Created Odoo partner id=%s (%s)", partner_id, name)
    return partner_id


def _resolve_product_id(sku: str) -> int:
    """
    Map a Woo SKU -> Odoo product.product id. Note: sale order lines use
    product.product (variants), whereas product sync used product.template.
    Raise UnknownSkuError if not found (caller rejects the whole order).
    """
    ids = odoo.search("product.product", [["default_code", "=", sku]], limit=1)
    if not ids:
        raise UnknownSkuError(sku)
    return ids[0]


def _build_order_lines(order: dict) -> list:
    """
    Turn Woo line_items into Odoo order line commands.
    Validates ALL SKUs first; raises UnknownSkuError on the first miss so the
    whole order is rejected before anything is created.
    """
    line_items = order.get("line_items") or []
    if not line_items:
        raise ValueError("Order has no line items")

    lines = []
    for item in line_items:
        sku = item.get("sku")
        if not sku:
            raise UnknownSkuError("<missing sku on line item>")

        product_id = _resolve_product_id(sku)  # raises if unknown
        qty = float(item.get("quantity") or 0)
        # Unit price from Woo (price per unit, taxes handled by Odoo product/tax).
        unit_price = float(item.get("price") or 0)

        # (0, 0, {...}) is Odoo's "create a new line" command.
        lines.append((0, 0, {
            "product_id": product_id,
            "product_uom_qty": qty,
            "price_unit": unit_price,
        }))

    return lines


def import_wc_order_to_odoo(wc_order_id: int) -> int | None:
    """
    Main entry point (called from the FastAPI background task).
    Returns the Odoo sale.order id, or None if skipped/rejected.
    """
    # Idempotency: bail if we already imported this Woo order.
    existing = _existing_so_id(wc_order_id)
    if existing:
        logger.info("Woo order %s already imported as SO id=%s; skipping",
                    wc_order_id, existing)
        return existing

    order = _fetch_wc_order(wc_order_id)
    logger.info("Importing Woo order %s (status=%s)",
                wc_order_id, order.get("status"))

    # Validate + build lines FIRST. If any SKU is unknown, reject before
    # creating a partner or order (no partial state left behind).
    try:
        order_lines = _build_order_lines(order)
    except UnknownSkuError as e:
        logger.error("Rejecting Woo order %s: unknown SKU %s (no Odoo product)",
                     wc_order_id, e)
        return None
    except ValueError as e:
        logger.error("Rejecting Woo order %s: %s", wc_order_id, e)
        return None

    partner_id = _resolve_partner(order)

    so_values = {
        "partner_id": partner_id,
        "client_order_ref": f"WOO-{wc_order_id}",  # idempotency key + traceability
        "order_line": order_lines,
        # Left in 'draft' (quotation) on purpose -- not confirmed.
    }

    so_id = odoo.create("sale.order", so_values)
    logger.info("Created Odoo quotation SO id=%s for Woo order %s (partner=%s, %d lines)",
                so_id, wc_order_id, partner_id, len(order_lines))
    return so_id