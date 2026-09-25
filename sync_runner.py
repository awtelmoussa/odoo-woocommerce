"""
Unified Background Sync Runner.

Runs scheduled periodic background tasks:
  1. Stock Sync (Odoo -> WooCommerce) every STOCK_SYNC_INTERVAL_SECONDS (default 15m)
  2. Order Reconciliation (WooCommerce -> Odoo) every ORDER_RECONCILE_INTERVAL_SECONDS (default 1h)
"""

import logging
import time

from config import (
    STOCK_SYNC_INTERVAL_SECONDS,
    ORDER_RECONCILE_INTERVAL_SECONDS,
    ORDER_RECONCILE_HOURS,
)
from order_sync import reconcile_recent_orders
from stock_sync import run_stock_sync_once

logger = logging.getLogger("sync_runner")


def run_scheduler():
    logger.info("Starting Unified Background Sync Runner...")
    logger.info("  - Stock sync cadence: every %ds", STOCK_SYNC_INTERVAL_SECONDS)
    logger.info("  - Order reconcile cadence: every %ds (looking back %dh)", ORDER_RECONCILE_INTERVAL_SECONDS, ORDER_RECONCILE_HOURS)

    last_stock_sync = 0.0
    last_order_reconcile = 0.0

    while True:
        now = time.time()

        # 1. Stock Sync
        if now - last_stock_sync >= STOCK_SYNC_INTERVAL_SECONDS:
            try:
                logger.info("Executing scheduled stock sync...")
                run_stock_sync_once()
                last_stock_sync = now
            except Exception:
                logger.exception("Scheduled stock sync encountered an error.")

        # 2. Order Reconciliation
        if now - last_order_reconcile >= ORDER_RECONCILE_INTERVAL_SECONDS:
            try:
                logger.info("Executing scheduled order reconciliation...")
                reconcile_recent_orders(hours=ORDER_RECONCILE_HOURS)
                last_order_reconcile = now
            except Exception:
                logger.exception("Scheduled order reconciliation encountered an error.")

        time.sleep(10)  # Check every 10 seconds


if __name__ == "__main__":
    run_scheduler()