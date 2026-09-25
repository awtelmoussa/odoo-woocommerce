"""
FastAPI middleware: Odoo <-> WooCommerce sync webhooks.

Design rules enforced here:
  - Webhooks ACK instantly with 202 and offload real work to BackgroundTasks.
    The heavy sync (Odoo RPC + image upload + WooCommerce REST) NEVER runs on
    the request/response path, so the event loop is never blocked.
  - Dedupe/cooldown is in Redis (atomic, cross-worker, self-expiring), not an
    in-memory dict.
  - The sync code is synchronous (`requests`/XML-RPC), so we run it via
    run_in_threadpool from inside an ASYNC background task. FastAPI awaits
    coroutine background tasks, so the threadpool call is properly awaited and
    the sync actually executes.

Endpoints:
  GET  /                              -> health check
  GET  /health/redis                  -> reports whether Redis is reachable
  POST /webhook/odoo/product-updated  -> phase 1 (Odoo -> WooCommerce)
  POST /webhook/woocommerce-order     -> phase 2 (WooCommerce -> Odoo)
"""

import base64
import hashlib
import hmac
import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from config import (
    COOLDOWN_SECONDS,
    WC_ORDER_COOLDOWN_SECONDS,
    WC_WEBHOOK_SECRET,
)
from cooldown import acquire_cooldown, close_redis, ping_redis
from product_sync import sync_one_product_by_odoo_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI(title="Odoo <-> WooCommerce Sync")


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------
class OdooProductWebhook(BaseModel):
    # Odoo webhook sends the record id only. Accept either key just in case.
    id: int | None = None
    _id: int | None = None

    def product_id(self) -> int | None:
        return self.id or self._id


class WooOrderRef(BaseModel):
    id: int
    status: str | None = None


# --------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------
@app.get("/")
async def home():
    return {"status": "running"}


@app.get("/health/redis")
async def health_redis():
    """Quick probe so you can confirm Redis connectivity on a live server."""
    ok = await ping_redis()
    return {"redis": "up" if ok else "down"}


@app.on_event("shutdown")
async def _shutdown():
    await close_redis()


# --------------------------------------------------------------------------
# Phase 1: Odoo product -> WooCommerce
# --------------------------------------------------------------------------
def _run_product_sync(product_id: int) -> None:
    """Blocking worker (runs in threadpool). Never let it crash silently."""
    try:
        sync_one_product_by_odoo_id(product_id)
    except Exception:
        logger.exception("Product sync failed for Odoo id=%s", product_id)


async def _product_sync_task(product_id: int) -> None:
    """
    Async background task. FastAPI AWAITS coroutine background tasks, so the
    threadpool call below is properly awaited and the sync actually runs.
    """
    await run_in_threadpool(_run_product_sync, product_id)


@app.post("/webhook/odoo/product-updated", status_code=202)
async def product_updated(payload: OdooProductWebhook, background_tasks: BackgroundTasks):
    product_id = payload.product_id()
    if not product_id:
        return {"status": "skipped", "reason": "missing_id"}

    # Atomic, cross-worker cooldown. False -> still cooling down.
    if not await acquire_cooldown(f"product:{product_id}", COOLDOWN_SECONDS):
        return {"status": "skipped", "reason": "cooldown", "odoo_product_id": product_id}

    # Offload to threadpool via an async task; ACK immediately.
    background_tasks.add_task(_product_sync_task, product_id)
    logger.info("Accepted product webhook for Odoo id=%s", product_id)
    return {"status": "accepted", "odoo_product_id": product_id}


# --------------------------------------------------------------------------
# Phase 2: WooCommerce order -> Odoo Sales Order
# --------------------------------------------------------------------------
async def _verify_wc_signature(raw_body: bytes, signature: str | None) -> bool:
    """
    Verify the X-WC-Webhook-Signature header: base64(HMAC-SHA256(body, secret)).
    Returns False if no secret is configured or the signature is missing/invalid.
    """
    if not WC_WEBHOOK_SECRET or not signature:
        return False
    digest = hmac.new(WC_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def _run_order_import(wc_order_id: int) -> None:
    """Blocking worker: import a WooCommerce order into Odoo as a quotation."""
    try:
        from order_sync import import_wc_order_to_odoo
        import_wc_order_to_odoo(wc_order_id)
    except Exception:
        logger.exception("WooCommerce->Odoo import failed for order %s", wc_order_id)


async def _order_import_task(wc_order_id: int) -> None:
    await run_in_threadpool(_run_order_import, wc_order_id)


@app.post("/webhook/woocommerce-order", status_code=202)
async def woocommerce_order(
    request: Request,
    background_tasks: BackgroundTasks,
    x_wc_webhook_signature: str | None = Header(default=None),
):
    raw = await request.body()

    if not await _verify_wc_signature(raw, x_wc_webhook_signature):
        raise HTTPException(status_code=401, detail="invalid signature")

    order = WooOrderRef.model_validate_json(raw)

    # Fast guard against rapid duplicate deliveries / retries.
    if not await acquire_cooldown(f"wc-order:{order.id}", WC_ORDER_COOLDOWN_SECONDS):
        return {"status": "skipped", "reason": "duplicate", "wc_order_id": order.id}

    background_tasks.add_task(_order_import_task, order.id)
    logger.info("Accepted WooCommerce order webhook id=%s", order.id)
    return {"status": "accepted", "wc_order_id": order.id}