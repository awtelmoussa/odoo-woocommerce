from product_sync import sync_products


def run_full_sync():
    print("Starting Odoo → WooCommerce product sync...")

    sync_products(limit=50)

    print("Product sync finished.")


if __name__ == "__main__":
    run_full_sync()