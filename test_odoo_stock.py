"""Diagnostic test: verify Odoo inventory & stock quants."""

import sys
from odoo_client import OdooClient
from config import ODOO_URL, ODOO_DB

print(f"Connecting to Odoo ({ODOO_URL}, DB: {ODOO_DB})...")
odoo = OdooClient(lazy=True)

try:
    odoo.authenticate()
    print("Authentication successful!")

    products = odoo.search_read(
        "product.template",
        [],
        [
            "name",
            "default_code",
            "qty_available",
            "virtual_available",
            "free_qty",
        ],
        limit=20,
    )

    print(f"Inspecting {len(products)} products:")
    for p in products:
        print(
            p.get("name"),
            "| SKU:", p.get("default_code"),
            "| qty_available:", p.get("qty_available"),
            "| virtual_available:", p.get("virtual_available"),
            "| free_qty:", p.get("free_qty"),
        )
except Exception as e:
    print(f"\n[ERROR] Could not connect to Odoo: {e}")
    print("Please verify ODOO_URL, ODOO_DB, ODOO_USERNAME, and ODOO_PASSWORD in your .env file.")
    sys.exit(1)