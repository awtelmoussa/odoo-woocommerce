"""Diagnostic test: verify Odoo product read access."""

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
            "list_price",
            "categ_id",
        ],
        limit=10,
    )

    print(f"Found {len(products)} products:")
    for p in products:
        print(
            p.get("name"),
            "| SKU:", p.get("default_code"),
            "| Price:", p.get("list_price"),
            "| Category:", p.get("categ_id"),
        )
except Exception as e:
    print(f"\n[ERROR] Could not connect to Odoo: {e}")
    print("Please verify ODOO_URL, ODOO_DB, ODOO_USERNAME, and ODOO_PASSWORD in your .env file.")
    sys.exit(1)