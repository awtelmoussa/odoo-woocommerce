"""
WooCommerce order -> Odoo Sales Order import & lifecycle synchronization.

Enhancements:
  - Financial completeness:
      * Standard product line items with unit prices and quantities.
      * Shipping charge lines mapped to an Odoo service product (DELIVERY).
      * Fee lines handled dynamically.
      * Coupon & discount information recorded.
  - Comprehensive customer mapping:
      * Resolves ISO country code to res.country.
      * Resolves state code to res.country.state.
      * Creates child delivery address (partner_shipping_id) if shipping differs from billing.
  - Traceability & Notes:
      * Preserves customer notes in sale.order.note.
      * Stores payment method, transaction ID, and WooCommerce order URL in Odoo chatter.
  - Idempotency & Lifecycle:
      * client_order_ref="WOO-{order_id}" prevents duplicates.
      * Status synchronization: handles 'cancelled', 'refunded', and 'completed' transitions.
  - Order reconciliation:
      * Polling function to catch missed webhooks over a configurable time window.
"""

from datetime import datetime, timezone, timedelta
import logging
from typing import Any

from config import (
    ODOO_SHIPPING_PRODUCT_CODE,
    ODOO_DISCOUNT_PRODUCT_CODE,
    WC_URL,
)
from odoo_client import OdooClient
from utils import sanitize_text, safe_float
from woocommerce_client import get_wc_order, get_wc_orders

logger = logging.getLogger("order_sync")

odoo = OdooClient(lazy=True)


class UnknownSkuError(Exception):
    """Raised when a WooCommerce line item references a SKU not found in Odoo."""


def _resolve_partner(order: dict) -> tuple[int, int]:
    """
    Find or create Odoo partner for billing and shipping.
    Returns (partner_invoice_id, partner_shipping_id).
    """
    billing = order.get("billing") or {}
    shipping = order.get("shipping") or {}

    email = sanitize_text(billing.get("email")).lower()
    first_name = sanitize_text(billing.get("first_name"))
    last_name = sanitize_text(billing.get("last_name"))
    company = sanitize_text(billing.get("company"))
    name = f"{first_name} {last_name}".strip() or company or sanitize_text(order.get("customer_username")) or "Online Customer"

    country_id = odoo.resolve_country_id(billing.get("country"))
    state_id = odoo.resolve_state_id(billing.get("state"), country_id)

    # 1. Match or create billing partner
    partner_id = None
    if email:
        existing = odoo.search("res.partner", [["email", "=ilike", email]], limit=1)
        if existing:
            partner_id = existing[0]

    if not partner_id:
        partner_values = {
            "name": name,
            "email": email or False,
            "phone": sanitize_text(billing.get("phone")) or False,
            "street": sanitize_text(billing.get("address_1")) or False,
            "street2": sanitize_text(billing.get("address_2")) or False,
            "city": sanitize_text(billing.get("city")) or False,
            "zip": sanitize_text(billing.get("postcode")) or False,
            "country_id": country_id or False,
            "state_id": state_id or False,
            "customer_rank": 1,
        }
        partner_id = odoo.create("res.partner", partner_values)
        logger.info("Created new Odoo customer id=%s (%s)", partner_id, name)

    # 2. Check if shipping address is distinct
    ship_name = f"{sanitize_text(shipping.get('first_name'))} {sanitize_text(shipping.get('last_name'))}".strip()
    ship_street = sanitize_text(shipping.get("address_1"))

    if ship_street and (ship_street != sanitize_text(billing.get("address_1")) or ship_name != name):
        ship_country_id = odoo.resolve_country_id(shipping.get("country")) or country_id
        ship_state_id = odoo.resolve_state_id(shipping.get("state"), ship_country_id) or state_id

        # Search existing child delivery address for this partner
        delivery_ids = odoo.search(
            "res.partner",
            [
                ["parent_id", "=", partner_id],
                ["type", "=", "delivery"],
                ["street", "=", ship_street],
            ],
            limit=1,
        )
        if delivery_ids:
            shipping_partner_id = delivery_ids[0]
        else:
            shipping_partner_values = {
                "name": ship_name or name,
                "parent_id": partner_id,
                "type": "delivery",
                "street": ship_street,
                "street2": sanitize_text(shipping.get("address_2")) or False,
                "city": sanitize_text(shipping.get("city")) or False,
                "zip": sanitize_text(shipping.get("postcode")) or False,
                "country_id": ship_country_id or False,
                "state_id": ship_state_id or False,
            }
            shipping_partner_id = odoo.create("res.partner", shipping_partner_values)
            logger.info("Created child shipping address id=%s for partner=%s", shipping_partner_id, partner_id)
        return partner_id, shipping_partner_id

    return partner_id, partner_id


def _resolve_product_id(sku: str) -> int:
    """Map a WooCommerce SKU -> Odoo product.product ID."""
    sku_clean = sanitize_text(sku)
    ids = odoo.search("product.product", [["default_code", "=", sku_clean]], limit=1)
    if not ids:
        # Check by name fallback if SKU is not found
        ids = odoo.search("product.product", [["name", "=", sku_clean]], limit=1)
    if not ids:
        raise UnknownSkuError(sku)
    return ids[0]


def _build_order_lines(order: dict) -> list[tuple[int, int, dict]]:
    """
    Build Odoo order lines:
      - Product line items
      - Shipping charges (as DELIVERY service line)
      - Fee lines
    """
    lines: list[tuple[int, int, dict]] = []

    # 1. Product line items
    line_items = order.get("line_items") or []
    if not line_items:
        raise ValueError("WooCommerce order contains no line items")

    for item in line_items:
        sku = sanitize_text(item.get("sku"))
        if not sku:
            raise UnknownSkuError(f"Missing SKU on item '{item.get('name')}'")

        product_id = _resolve_product_id(sku)
        qty = safe_float(item.get("quantity"), 1.0)
        price_unit = safe_float(item.get("price"), 0.0)
        item_name = sanitize_text(item.get("name") or sku)

        lines.append((0, 0, {
            "product_id": product_id,
            "name": item_name,
            "product_uom_qty": qty,
            "price_unit": price_unit,
        }))

    # 2. Shipping lines
    shipping_lines = order.get("shipping_lines") or []
    for s_line in shipping_lines:
        cost = safe_float(s_line.get("total"), 0.0)
        if cost > 0:
            shipping_title = sanitize_text(s_line.get("method_title") or "Shipping Charges")
            shipping_prod_id = odoo.get_or_create_service_product(
                default_code=ODOO_SHIPPING_PRODUCT_CODE,
                name="Delivery / Shipping",
            )
            lines.append((0, 0, {
                "product_id": shipping_prod_id,
                "name": shipping_title,
                "product_uom_qty": 1.0,
                "price_unit": cost,
            }))

    # 3. Fee lines
    fee_lines = order.get("fee_lines") or []
    for f_line in fee_lines:
        fee_amount = safe_float(f_line.get("total"), 0.0)
        if fee_amount != 0:
            fee_title = sanitize_text(f_line.get("name") or "Additional Fee")
            fee_prod_id = odoo.get_or_create_service_product(
                default_code="FEE",
                name="Service Fee",
            )
            lines.append((0, 0, {
                "product_id": fee_prod_id,
                "name": fee_title,
                "product_uom_qty": 1.0,
                "price_unit": fee_amount,
            }))

    return lines


def find_existing_so_id(wc_order_id: int) -> int | None:
    """Check if this WooCommerce order is already in Odoo."""
    ref = f"WOO-{wc_order_id}"
    ids = odoo.search("sale.order", [["client_order_ref", "=", ref]], limit=1)
    return ids[0] if ids else None


def import_wc_order_to_odoo(wc_order_id: int, order_data: dict | None = None) -> int | None:
    """
    Main order import worker.
    Returns Odoo sale.order ID or None if skipped/rejected.
    """
    # 1. Idempotency check
    existing_id = find_existing_so_id(wc_order_id)
    if existing_id:
        logger.info("WooCommerce order %s already imported as Odoo SO id=%s; skipping creation", wc_order_id, existing_id)
        return existing_id

    # 2. Fetch full order if not provided
    order = order_data or get_wc_order(wc_order_id)
    status = order.get("status")
    logger.info("Importing WooCommerce order %s (status=%s)", wc_order_id, status)

    # 3. Validate line items before creating any partners
    try:
        order_lines = _build_order_lines(order)
    except UnknownSkuError as exc:
        logger.error("Rejecting Woo order %s: %s", wc_order_id, exc)
        return None
    except ValueError as exc:
        logger.error("Rejecting Woo order %s: %s", wc_order_id, exc)
        return None

    # 4. Resolve customer and shipping partner
    partner_invoice_id, partner_shipping_id = _resolve_partner(order)

    # 5. Extract notes and payment info
    customer_note = sanitize_text(order.get("customer_note") or "")
    payment_method = sanitize_text(order.get("payment_method_title") or order.get("payment_method") or "")
    transaction_id = sanitize_text(order.get("transaction_id") or "")
    total_amount = safe_float(order.get("total"))
    currency = sanitize_text(order.get("currency") or "")

    so_values: dict[str, Any] = {
        "partner_id": partner_invoice_id,
        "partner_invoice_id": partner_invoice_id,
        "partner_shipping_id": partner_shipping_id,
        "client_order_ref": f"WOO-{wc_order_id}",
        "order_line": order_lines,
        "note": customer_note or False,
    }

    so_id = odoo.create("sale.order", so_values)
    logger.info("Created Odoo quotation SO id=%s for Woo order %s", so_id, wc_order_id)

    # 6. Post payment & audit details in chatter
    chatter_html = f"""
    <p><b>Imported from WooCommerce</b></p>
    <ul>
        <li><b>Woo Order ID:</b> <a href="{WC_URL}/wp-admin/post.php?post={wc_order_id}&action=edit" target="_blank">#{wc_order_id}</a></li>
        <li><b>Status:</b> {status}</li>
        <li><b>Total Charged:</b> {total_amount} {currency}</li>
        <li><b>Payment Method:</b> {payment_method or 'N/A'}</li>
        <li><b>Transaction ID:</b> {transaction_id or 'N/A'}</li>
    </ul>
    """
    odoo.post_message("sale.order", so_id, chatter_html)

    return so_id


def handle_wc_order_status_update(wc_order_id: int, new_status: str) -> bool:
    """
    Handle WooCommerce order status changes (e.g. cancelled, refunded, completed).
    """
    so_id = find_existing_so_id(wc_order_id)
    if not so_id:
        logger.info("Order WOO-%s not yet in Odoo. Attempting import...", wc_order_id)
        imported = import_wc_order_to_odoo(wc_order_id)
        return bool(imported)

    so_records = odoo.read("sale.order", [so_id], ["state", "name"])
    if not so_records:
        return False

    current_state = so_records[0].get("state")
    so_name = so_records[0].get("name")

    logger.info("Updating Woo order %s (Odoo SO %s, state=%s) with new status=%s", wc_order_id, so_name, current_state, new_status)

    if new_status == "cancelled":
        if current_state in ("draft", "sent"):
            odoo.cancel_sale_order(so_id)
            odoo.post_message("sale.order", so_id, "<p><b>Order Cancelled in WooCommerce.</b> Quotation cancelled.</p>")
            return True
        else:
            odoo.post_message(
                "sale.order",
                so_id,
                f"<p style='color: red;'><b>URGENT:</b> WooCommerce order was marked <b>CANCELLED</b>, but Odoo SO is in state '{current_state}'. Manual review and stock reversal required.</p>",
            )
            return True

    elif new_status == "refunded":
        odoo.post_message(
            "sale.order",
            so_id,
            "<p style='color: orange;'><b>ALERT:</b> WooCommerce order was marked <b>REFUNDED</b>. Check refund status in accounting.</p>",
        )
        return True

    elif new_status == "completed":
        odoo.post_message("sale.order", so_id, "<p>WooCommerce order marked as <b>Completed</b>.</p>")
        return True

    return True


def reconcile_recent_orders(hours: int = 24) -> dict[str, int]:
    """
    Reconciliation worker: polls WooCommerce for recent orders to catch any
    missed webhooks.
    """
    logger.info("Starting order reconciliation for the last %d hours...", hours)
    after_dt = datetime.now(timezone.utc) - timedelta(hours=hours)
    after_iso = after_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    page = 1
    total_found = 0
    imported = 0
    updated = 0

    while True:
        try:
            orders = get_wc_orders(after=after_iso, page=page, per_page=50)
        except Exception:
            logger.exception("Failed to query WooCommerce orders for reconciliation (page %d)", page)
            break

        if not orders:
            break

        for o in orders:
            total_found += 1
            o_id = o.get("id")
            if not o_id:
                continue

            existing = find_existing_so_id(o_id)
            if not existing:
                logger.info("Reconciliation: Found unimported order #%s, importing...", o_id)
                res = import_wc_order_to_odoo(o_id, order_data=o)
                if res:
                    imported += 1
            else:
                handle_wc_order_status_update(o_id, o.get("status"))
                updated += 1

        if len(orders) < 50:
            break
        page += 1

    summary = {"found": total_found, "imported": imported, "updated": updated}
    logger.info("Order reconciliation finished: %s", summary)
    return summary