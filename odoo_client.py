"""
Odoo XML-RPC client.

CRITICAL fix vs. the original:
  xmlrpc.client.ServerProxy has NO timeout by default -> it waits forever.
  Every call here crosses the public internet to Odoo; one stalled call would
  hang the worker thread permanently, and under load all threads hang and the
  app dies silently. xmlrpc has no `timeout=` arg, so we attach a custom
  Transport whose underlying HTTP(S) connection enforces a socket timeout.

Also added:
  - read()         : standard Odoo read(model, ids, fields)
  - error logging  : failed execute_kw logs the cause instead of a bare Fault
"""

import http.client
import logging
import xmlrpc.client

from config import (
    ODOO_URL,
    ODOO_DB,
    ODOO_USERNAME,
    ODOO_PASSWORD,
    HTTP_CONNECT_TIMEOUT,
    HTTP_READ_TIMEOUT,
)

logger = logging.getLogger("odoo_client")

# A single read timeout for the XML-RPC socket. We use the larger of the two
# configured values so a legitimately slow query (e.g. reading an image field)
# is not killed prematurely, while still being finite.
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
    def __init__(self):
        self.common = _build_proxy(f"{ODOO_URL}/xmlrpc/2/common")
        self.models = _build_proxy(f"{ODOO_URL}/xmlrpc/2/object")

        try:
            self.uid = self.common.authenticate(
                ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {}
            )
        except Exception:
            logger.exception("Odoo authentication request failed")
            raise

        if not self.uid:
            raise RuntimeError("Odoo authentication failed (bad credentials/db)")

        logger.info("Odoo authenticated as uid=%s", self.uid)

    def _execute(self, model: str, method: str, args: list, kwargs: dict | None = None):
        """Single choke point for execute_kw, with timeout + error logging."""
        try:
            return self.models.execute_kw(
                ODOO_DB,
                self.uid,
                ODOO_PASSWORD,
                model,
                method,
                args,
                kwargs or {},
            )
        except Exception:
            logger.exception("Odoo %s.%s failed (args=%s)", model, method, args)
            raise

    def search_read(self, model, domain=None, fields=None, limit=100):
        return self._execute(
            model,
            "search_read",
            [domain or []],
            {"fields": fields or [], "limit": limit},
        )

    def read(self, model, ids, fields=None):
        """Standard Odoo read(model, ids, fields) by record id(s)."""
        if isinstance(ids, int):
            ids = [ids]
        return self._execute(
            model,
            "read",
            [ids],
            {"fields": fields or []},
        )

    def search(self, model, domain=None, limit=1):
        """Return a list of record ids matching domain (lightweight, no fields)."""
        return self._execute(
            model,
            "search",
            [domain or []],
            {"limit": limit},
        )

    def create(self, model, values: dict):
        """Create one record; returns the new record id."""
        return self._execute(model, "create", [values])

    def write(self, model, ids, values: dict):
        """Update existing record(s). Returns True on success."""
        if isinstance(ids, int):
            ids = [ids]
        return self._execute(model, "write", [ids, values])