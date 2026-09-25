"""
FastAPI middleware: Odoo <-> WooCommerce sync webhooks & management API.

Enhancements:
  - Modern FastAPI lifespan context manager for clean startup & shutdown.
  - HMAC-SHA256 signature verification for WooCommerce webhooks.
  - Token authentication for incoming Odoo webhooks (ODOO_WEBHOOK_SECRET).
  - Handles both order creation and status transitions (cancelled, refunded, completed).
  - Immediate 202 ACK with non-blocking execution via background threadpools.
  - Redis cooldown deduplication.
  - Comprehensive health check probing Redis, Odoo, and WooCommerce.
  - Manual sync trigger endpoints for operations and testing.
"""

import base64
from contextlib import asynccontextmanager
import hashlib
import hmac
import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from config import (
    COOLDOWN_SECONDS,
    WC_ORDER_COOLDOWN_SECONDS,
    WC_WEBHOOK_SECRET,
    ODOO_WEBHOOK_SECRET,
    ORDER_RECONCILE_HOURS,
)
from cooldown import acquire_cooldown, close_redis, ping_redis
from odoo_client import OdooClient
from order_sync import import_wc_order_to_odoo, handle_wc_order_status_update, reconcile_recent_orders
from product_sync import sync_one_product, sync_products
from stock_sync import run_stock_sync_once
from woocommerce_client import wc_get

logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Odoo <-> WooCommerce Sync Gateway...")
    yield
    logger.info("Shutting down Odoo <-> WooCommerce Sync Gateway...")
    await close_redis()


app = FastAPI(title="Odoo <-> WooCommerce Sync Gateway", lifespan=lifespan)


# --------------------------------------------------------------------------
# Request Models
# --------------------------------------------------------------------------
class OdooProductWebhook(BaseModel):
    id: int | None = None
    _id: int | None = None
    sku: str | None = None

    def identifier(self) -> int | str | None:
        return self.id or self._id or self.sku


# --------------------------------------------------------------------------
# Health & Status
# --------------------------------------------------------------------------
@app.get("/")
async def home():
    return {"status": "running", "service": "Odoo-WooCommerce Sync Gateway"}


@app.get("/health")
async def health_check():
    """Comprehensive health check across all integrated services."""
    redis_ok = await ping_redis()

    def check_odoo():
        try:
            return OdooClient(lazy=False).is_connected()
        except Exception:
            return False

    def check_woo():
        try:
            wc_get("/wp-json/wc/v3/system_status")
            return True
        except Exception:
            return False

    odoo_ok = await run_in_threadpool(check_odoo)
    woo_ok = await run_in_threadpool(check_woo)

    overall_status = "healthy" if (redis_ok and odoo_ok and woo_ok) else "degraded"

    return {
        "status": overall_status,
        "services": {
            "redis": "up" if redis_ok else "down",
            "odoo": "up" if odoo_ok else "down",
            "woocommerce": "up" if woo_ok else "down",
        },
    }


# --------------------------------------------------------------------------
# Phase 1: Odoo product -> WooCommerce
# --------------------------------------------------------------------------
def _run_product_sync(identifier: int | str) -> None:
    try:
        sync_one_product(identifier)
    except Exception:
        logger.exception("Product sync failed for identifier=%s", identifier)


@app.post("/webhook/odoo/product-updated", status_code=status.HTTP_202_ACCEPTED)
async def odoo_product_updated(
    payload: OdooProductWebhook,
    background_tasks: BackgroundTasks,
    x_odoo_secret: str | None = Header(default=None),
    secret: str | None = Query(default=None),
):
    # Optional shared secret validation
    if ODOO_WEBHOOK_SECRET:
        token = x_odoo_secret or secret
        if not token or not hmac.compare_digest(token, ODOO_WEBHOOK_SECRET):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Odoo webhook secret")

    identifier = payload.identifier()
    if not identifier:
        return {"status": "skipped", "reason": "missing_identifier"}

    # Cooldown check
    if not await acquire_cooldown(f"product:{identifier}", COOLDOWN_SECONDS):
        return {"status": "skipped", "reason": "cooldown", "identifier": identifier}

    background_tasks.add_task(run_in_threadpool, _run_product_sync, identifier)
    logger.info("Accepted product webhook for identifier=%s", identifier)
    return {"status": "accepted", "identifier": identifier}


# --------------------------------------------------------------------------
# Phase 2: WooCommerce order -> Odoo Sales Order
# --------------------------------------------------------------------------
async def _verify_wc_signature(raw_body: bytes, signature: str | None) -> bool:
    """Verify X-WC-Webhook-Signature HMAC SHA-256."""
    if not WC_WEBHOOK_SECRET or not signature:
        return False
    digest = hmac.new(WC_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def _process_wc_order_event(order_data: dict, topic: str | None) -> None:
    """Worker handling both order creation and status changes."""
    wc_order_id = order_data.get("id")
    if not wc_order_id:
        return

    order_status = order_data.get("status")
    logger.info("Processing Woo order #%s event (topic=%s, status=%s)", wc_order_id, topic, order_status)

    try:
        # If this is a status update or cancelled/refunded order, route to status handler
        if topic and ("status_changed" in topic or "updated" in topic):
            handle_wc_order_status_update(wc_order_id, order_status)
        else:
            # Creation flow
            import_wc_order_to_odoo(wc_order_id, order_data=order_data)
    except Exception:
        logger.exception("Failed processing WooCommerce order #%s", wc_order_id)


@app.post("/webhook/woocommerce-order", status_code=status.HTTP_202_ACCEPTED)
async def woocommerce_order(
    request: Request,
    background_tasks: BackgroundTasks,
    x_wc_webhook_signature: str | None = Header(default=None),
    x_wc_webhook_topic: str | None = Header(default=None),
):
    raw_body = await request.body()

    # Validate HMAC signature
    if not await _verify_wc_signature(raw_body, x_wc_webhook_signature):
        logger.warning("Rejected WooCommerce webhook with invalid signature")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    import json
    try:
        order_data = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body")

    order_id = order_data.get("id")
    if not order_id:
        return {"status": "skipped", "reason": "missing_order_id"}

    # Cooldown deduplication
    if not await acquire_cooldown(f"wc-order:{order_id}", WC_ORDER_COOLDOWN_SECONDS):
        return {"status": "skipped", "reason": "cooldown", "wc_order_id": order_id}

    background_tasks.add_task(run_in_threadpool, _process_wc_order_event, order_data, x_wc_webhook_topic)
    logger.info("Accepted WooCommerce order webhook #%s", order_id)
    return {"status": "accepted", "wc_order_id": order_id}


# --------------------------------------------------------------------------
# Manual Operations / Sync Triggers
# --------------------------------------------------------------------------
@app.post("/sync/products", status_code=status.HTTP_202_ACCEPTED)
async def trigger_product_sync(background_tasks: BackgroundTasks, limit: int = 50, offset: int = 0):
    background_tasks.add_task(run_in_threadpool, sync_products, limit, offset)
    return {"status": "accepted", "message": f"Product sync triggered (limit={limit}, offset={offset})"}


@app.post("/sync/stock", status_code=status.HTTP_202_ACCEPTED)
async def trigger_stock_sync(background_tasks: BackgroundTasks, refresh_cache: bool = False):
    background_tasks.add_task(run_in_threadpool, run_stock_sync_once, refresh_cache)
    return {"status": "accepted", "message": "Stock sync triggered"}


@app.post("/sync/orders/reconcile", status_code=status.HTTP_202_ACCEPTED)
async def trigger_order_reconcile(background_tasks: BackgroundTasks, hours: int = ORDER_RECONCILE_HOURS):
    background_tasks.add_task(run_in_threadpool, reconcile_recent_orders, hours)
    return {"status": "accepted", "message": f"Order reconciliation triggered for last {hours} hours"}