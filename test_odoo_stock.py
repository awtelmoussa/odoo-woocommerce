from odoo_client import OdooClient

odoo = OdooClient()

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
    limit=20
)

for p in products:
    print(
        p["name"],
        "| SKU:", p["default_code"],
        "| qty_available:", p.get("qty_available"),
        "| virtual_available:", p.get("virtual_available"),
        "| free_qty:", p.get("free_qty"),
    )