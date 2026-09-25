"""
Central configuration. All values come from environment variables (.env).

Notable additions for resource safety:
  - REDIS_URL              -> distributed cooldown store
  - HTTP timeouts          -> no external call is allowed to hang forever
  - COOLDOWN_SECONDS       -> webhook dedupe window
  - STOCK_SYNC_*           -> periodic stock sync cadence / batch size
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


# --- Odoo ---------------------------------------------------------------
ODOO_URL = _required("ODOO_URL")
ODOO_DB = _required("ODOO_DB")
ODOO_USERNAME = _required("ODOO_USERNAME")
ODOO_PASSWORD = _required("ODOO_PASSWORD")

# --- WooCommerce REST ---------------------------------------------------
WC_URL = _required("WC_URL").rstrip("/")
WC_CONSUMER_KEY = _required("WC_CONSUMER_KEY")
WC_CONSUMER_SECRET = _required("WC_CONSUMER_SECRET")

# --- WordPress media (image upload) ------------------------------------
WP_USERNAME = _required("WP_USERNAME")
WP_APP_PASSWORD = _required("WP_APP_PASSWORD")

# --- WooCommerce -> Odoo webhook (next phase) --------------------------
# Shared secret configured in the WooCommerce webhook settings; used to
# verify the X-WC-Webhook-Signature HMAC. Optional until phase 2 is enabled.
WC_WEBHOOK_SECRET = os.getenv("WC_WEBHOOK_SECRET", "")

# --- Redis (distributed cooldown / dedupe) -----------------------------
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# --- Cooldown windows ---------------------------------------------------
COOLDOWN_SECONDS = int(os.getenv("COOLDOWN_SECONDS", "15"))
WC_ORDER_COOLDOWN_SECONDS = int(os.getenv("WC_ORDER_COOLDOWN_SECONDS", "30"))

# --- Periodic stock sync ------------------------------------------------
# How often the standalone stock_sync.py loop runs (seconds). Default 15 min.
STOCK_SYNC_INTERVAL_SECONDS = int(os.getenv("STOCK_SYNC_INTERVAL_SECONDS", "900"))
# How many products to pull from Odoo per cycle. Raise if your catalog is big.
STOCK_SYNC_LIMIT = int(os.getenv("STOCK_SYNC_LIMIT", "1000"))

# --- HTTP timeouts (seconds) -------------------------------------------
# (connect_timeout, read_timeout). NEVER None / infinite.
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "10"))
HTTP_READ_TIMEOUT = float(os.getenv("HTTP_READ_TIMEOUT", "30"))
# Images can be large; allow a longer read window for the media upload only.
IMAGE_READ_TIMEOUT = float(os.getenv("IMAGE_READ_TIMEOUT", "60"))

# Tuple form expected by the `requests` library.
HTTP_TIMEOUT = (HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)
IMAGE_TIMEOUT = (HTTP_CONNECT_TIMEOUT, IMAGE_READ_TIMEOUT)

DEBUG = os.getenv("DEBUG", "false").lower() == "true"

