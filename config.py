"""
Central configuration. All values come from environment variables (.env).

Notable additions for resource safety & multi-entity reliability:
  - REDIS_URL                   -> distributed cooldown & caching store
  - HTTP timeouts               -> no external call is allowed to hang forever
  - COOLDOWN_SECONDS            -> webhook dedupe window
  - STOCK_SYNC_*                -> periodic stock sync cadence / batch size
  - ODOO_SHIPPING_PRODUCT_CODE  -> Odoo SKU for shipping charge line
  - ODOO_DISCOUNT_PRODUCT_CODE  -> Odoo SKU for coupon/discount line
  - ODOO_DEFAULT_COUNTRY_CODE   -> fallback partner country if missing
  - ODOO_WEBHOOK_SECRET         -> token to authenticate incoming Odoo webhooks
"""

import os
import logging
from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# --- Odoo ---------------------------------------------------------------
ODOO_URL = _required("ODOO_URL").rstrip("/")
ODOO_DB = _required("ODOO_DB")
ODOO_USERNAME = _required("ODOO_USERNAME")
ODOO_PASSWORD = _required("ODOO_PASSWORD")

# Optional Odoo inventory / accounting settings
ODOO_WAREHOUSE_ID = int(os.getenv("ODOO_WAREHOUSE_ID")) if os.getenv("ODOO_WAREHOUSE_ID") else None
ODOO_LOCATION_ID = int(os.getenv("ODOO_LOCATION_ID")) if os.getenv("ODOO_LOCATION_ID") else None
ODOO_SHIPPING_PRODUCT_CODE = os.getenv("ODOO_SHIPPING_PRODUCT_CODE", "DELIVERY")
ODOO_DISCOUNT_PRODUCT_CODE = os.getenv("ODOO_DISCOUNT_PRODUCT_CODE", "DISCOUNT")
ODOO_DEFAULT_COUNTRY_CODE = os.getenv("ODOO_DEFAULT_COUNTRY_CODE", "LB")
ODOO_WEBHOOK_SECRET = os.getenv("ODOO_WEBHOOK_SECRET", "")

# --- WooCommerce REST ---------------------------------------------------
WC_URL = _required("WC_URL").rstrip("/")
WC_CONSUMER_KEY = _required("WC_CONSUMER_KEY")
WC_CONSUMER_SECRET = _required("WC_CONSUMER_SECRET")

# --- WordPress media (image upload) ------------------------------------
WP_USERNAME = _required("WP_USERNAME")
WP_APP_PASSWORD = _required("WP_APP_PASSWORD")

# --- WooCommerce -> Odoo webhook ---------------------------------------
WC_WEBHOOK_SECRET = os.getenv("WC_WEBHOOK_SECRET", "")

# --- Redis (distributed cooldown / dedupe / cache) ---------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# --- Cooldown windows (seconds) ----------------------------------------
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "15"))
WC_ORDER_COOLDOWN_SECONDS = int(os.getenv("WC_ORDER_COOLDOWN_SECONDS", "30"))

# --- Periodic stock sync ------------------------------------------------
STOCK_SYNC_INTERVAL_SECONDS = int(os.getenv("STOCK_SYNC_INTERVAL_SECONDS", "900"))
STOCK_SYNC_LIMIT = int(os.getenv("STOCK_SYNC_LIMIT", "1000"))
STOCK_CACHE_TTL = int(os.getenv("STOCK_CACHE_TTL", "3600"))  # 1 hour cache for WC SKU map

# --- Periodic order reconciliation -------------------------------------
ORDER_RECONCILE_INTERVAL_SECONDS = int(os.getenv("ORDER_RECONCILE_INTERVAL_SECONDS", "3600"))
ORDER_RECONCILE_HOURS = int(os.getenv("ORDER_RECONCILE_HOURS", "24"))

# --- HTTP timeouts (seconds) -------------------------------------------
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "10"))
HTTP_READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", "30"))
IMAGE_READ_TIMEOUT = float(os.getenv("IMAGE_READ_TIMEOUT", "60"))

HTTP_TIMEOUT = (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)
IMAGE_TIMEOUT = (HTTP_CONNECT_TIMEOUT, IMAGE_READ_TIMEOUT)

# --- General -----------------------------------------------------------
DEBUG = os.getenv("DEBUG", "false").lower() == "true"
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG" if DEBUG else "INFO").upper()

# Configure logging format
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
