from odoo_client import OdooClient

odoo = OdooClient()

products = odoo.search_read(
    "product.template",
    [],
    [
        "name",
        "default_code",
        "list_price",
        "categ_id",
    ]
)

for p in products:
    print(
        p["name"],
        "| SKU:", p["default_code"],
        "| Price:", p["list_price"],
        "| Category:", p["categ_id"]
    )