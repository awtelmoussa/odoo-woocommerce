"""
Odoo XML-RPC client.

Enhancements:
  - Lazy connection: does not crash on import if Odoo is momentarily starting up.
  - Auto-reconnect & session recovery on expired credentials.
  - Automatic retry with backoff on transient network failures.
  - Timeout socket transport (never hangs indefinitely).
  - Paged search_read_all helper to avoid 1000-record query caps.
  - Odoo entity helpers: country/state lookup, partner creation, service product creation, chatter notes, and order cancellation.
"""

import http.client
import logging
import socket
import time
import xmlrpc.client
from typing import Any

from config import (
    ODOO_URL,
    ODOO_DB,
    ODOO_USERNAME,
    ODOO_PASSWORD,
    HTTP_CONNECT_TIMEOUT,
    HTTP_READ_TIMEOUT,
    ODOO_DEFAULT_COUNTRY_CODE,
)
from utils import sanitize_text

logger = logging.getLogger("odoo_client")

_RPC_TIMEOUT = max(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT)


class _TimeoutTransport(xmlrpc.client.Transport):
    """HTTP transport that enforces a socket timeout on the connection."""

    def __init__(self, timeout: float, use_datetime: bool = False):
        super().__init__(use_datetime=use_datetime)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    """HTTPS variant of the timeout transport (for https:// Odoo URLs)."""

    def __init__(self, timeout: float, use_datetime: bool = False):
        super().__init__(use_datetime=use_datetime)
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn


def _build_proxy(url: str) -> xmlrpc.client.ServerProxy:
    """Build a ServerProxy with the right timeout transport for http/https."""
    if url.lower().startswith("https"):
        transport = _TimeoutSafeTransport(_RPC_TIMEOUT)
    else:
        transport = _TimeoutTransport(_RPC_TIMEOUT)
    return xmlrpc.client.ServerProxy(url, transport=transport, allow_none=True)


class OdooClient:
    def __init__(self, lazy: bool = True):
        self.common = _build_proxy(f"{ODOO_URL}/xmlrpc/2/common")
        self.models = _build_proxy(f"{ODOO_URL}/xmlrpc/2/object")
        self.uid: int | None = None
        self._country_cache: dict[str, int] = {}
        self._state_cache: dict[str, int] = {}

        if not lazy:
            self.authenticate()

    def authenticate(self, force: bool = False) -> int:
        """Authenticate with Odoo and cache the UID."""
        if self.uid and not force:
            return self.uid

        try:
            uid = self.common.authenticate(
                ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {}
            )
            if not uid:
                raise RuntimeError(
                    f"Odoo authentication failed for db='{ODOO_DB}', user='{ODOO_USERNAME}'"
                )
            self.uid = uid
            logger.info("Odoo authenticated successfully as uid=%s", self.uid)
            return self.uid
        except Exception:
            logger.exception("Failed to authenticate with Odoo at %s", ODOO_URL)
            raise

    def is_connected(self) -> bool:
        """Test if Odoo is reachable and credentials are valid."""
        try:
            self.authenticate(force=True)
            return True
        except Exception:
            return False

    def _execute(self, model: str, method: str, args: list, kwargs: dict | None = None, retries: int = 2):
        """
        Execute an Odoo XML-RPC method with automatic re-authentication and retries.
        """
        self.authenticate()
        kwargs = kwargs or {}

        for attempt in range(retries + 1):
            try:
                return self.models.execute_kw(
                    ODOO_DB,
                    self.uid,
                    ODOO_PASSWORD,
                    model,
                    method,
                    args,
                    kwargs,
                )
            except (socket.timeout, http.client.RemoteDisconnected, ConnectionError) as exc:
                if attempt < retries:
                    wait_time = 2 ** attempt
                    logger.warning("Network issue calling Odoo %s.%s (%s). Retrying in %ds...", model, method, exc, wait_time)
                    time.sleep(wait_time)
                    continue
                logger.exception("Odoo network error on %s.%s after %d retries", model, method, retries)
                raise
            except xmlrpc.client.Fault as fault:
                # If authentication failed or session dropped, try re-authenticating once
                if attempt == 0 and ("AccessDenied" in fault.faultString or "SessionExpired" in fault.faultString):
                    logger.warning("Odoo session invalid. Re-authenticating...")
                    self.authenticate(force=True)
                    continue
                logger.error("Odoo XML-RPC Fault %s.%s: %s", model, method, fault.faultString)
                raise
            except Exception:
                logger.exception("Unexpected error calling Odoo %s.%s (args=%s)", model, method, args)
                raise

    def search_read(self, model: str, domain: list | None = None, fields: list | None = None, limit: int = 100, offset: int = 0):
        return self._execute(
            model,
            "search_read",
            [domain or []],
            {"fields": fields or [], "limit": limit, "offset": offset},
        )

    def search_read_all(self, model: str, domain: list | None = None, fields: list | None = None, batch_size: int = 200) -> list[dict]:
        """
        Paginate through all matching records without hitting arbitrary limit caps.
        """
        all_records = []
        offset = 0
        domain = domain or []
        fields = fields or []

        while True:
            batch = self.search_read(model, domain=domain, fields=fields, limit=batch_size, offset=offset)
            if not batch:
                break
            all_records.extend(batch)
            if len(batch) < batch_size:
                break
            offset += batch_size

        return all_records

    def read(self, model: str, ids: int | list[int], fields: list | None = None):
        """Read specific fields for record ID(s)."""
        if isinstance(ids, int):
            ids = [ids]
        return self._execute(
            model,
            "read",
            [ids],
            {"fields": fields or []},
        )

    def search(self, model: str, domain: list | None = None, limit: int = 1, offset: int = 0) -> list[int]:
        """Return list of matching record IDs."""
        return self._execute(
            model,
            "search",
            [domain or []],
            {"limit": limit, "offset": offset},
        )

    def create(self, model: str, values: dict) -> int:
        """Create a record in Odoo and return its new ID."""
        return self._execute(model, "create", [values])

    def write(self, model: str, ids: int | list[int], values: dict) -> bool:
        """Update existing record(s)."""
        if isinstance(ids, int):
            ids = [ids]
        return self._execute(model, "write", [ids, values])

    def post_message(self, model: str, res_id: int, body: str) -> None:
        """Add an internal note/message into the record's chatter."""
        try:
            self._execute(
                model,
                "message_post",
                [[res_id]],
                {"body": body, "message_type": "comment", "subtype_xmlid": "mail.mt_note"},
            )
        except Exception:
            logger.warning("Failed to post chatter message to %s id=%s", model, res_id, exc_info=True)

    def cancel_sale_order(self, order_id: int) -> bool:
        """Cancel a sales order in Odoo."""
        try:
            self._execute("sale.order", "action_cancel", [[order_id]])
            logger.info("Successfully cancelled Odoo sale order %s", order_id)
            return True
        except Exception:
            logger.exception("Failed to cancel Odoo sale order %s", order_id)
            return False

    # --------------------------------------------------------------------------
    # Helper Entities (Country, State, Partner, Service Products)
    # --------------------------------------------------------------------------
    def resolve_country_id(self, code: str | None) -> int | None:
        """Resolve Odoo res.country ID from ISO 2-letter country code."""
        if not code:
            code = ODOO_DEFAULT_COUNTRY_CODE
        code = code.strip().upper()
        if code in self._country_cache:
            return self._country_cache[code]

        ids = self.search("res.country", [["code", "=ilike", code]], limit=1)
        if ids:
            self._country_cache[code] = ids[0]
            return ids[0]
        return None

    def resolve_state_id(self, state_code: str | None, country_id: int | None) -> int | None:
        """Resolve Odoo res.country.state ID from state code and country ID."""
        if not state_code or not country_id:
            return None
        cache_key = f"{country_id}:{state_code.strip().upper()}"
        if cache_key in self._state_cache:
            return self._state_cache[cache_key]

        ids = self.search(
            "res.country.state",
            [["code", "=ilike", state_code.strip()], ["country_id", "=", country_id]],
            limit=1,
        )
        if not ids:
            # Also try matching by name
            ids = self.search(
                "res.country.state",
                [["name", "=ilike", state_code.strip()], ["country_id", "=", country_id]],
                limit=1,
            )

        if ids:
            self._state_cache[cache_key] = ids[0]
            return ids[0]
        return None

    def get_or_create_service_product(self, default_code: str, name: str, list_price: float = 0.0) -> int:
        """
        Get or create a service product in Odoo (for shipping lines, fees, discounts).
        Returns product.product ID.
        """
        ids = self.search("product.product", [["default_code", "=", default_code]], limit=1)
        if ids:
            return ids[0]

        values = {
            "name": name,
            "default_code": default_code,
            "type": "service",
            "list_price": list_price,
            "sale_ok": True,
            "purchase_ok": False,
        }
        product_id = self.create("product.product", values)
        logger.info("Created Odoo service product '%s' (SKU: %s) id=%s", name, default_code, product_id)
        return product_id