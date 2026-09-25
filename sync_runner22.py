import time
from product_sync import sync_products
from erpnext_client import (
    get_or_create_customer,
    create_sales_order,
    submit_sales_order,
    update_sales_order_status_note,
    add_sales_order_comment
)
from processed_orders import (
    is_order_processed,
    mark_order_processed,
    get_processed_order
)
from order_sync import get_latest_orders, get_order, build_order_payload


ORDER_SYNC_SECONDS = 60
PRODUCT_SYNC_SECONDS = 15 * 60


def sync_orders(limit=20):
    orders = get_latest_orders(limit=limit)

    print(f"Found {len(orders)} WooCommerce orders")

    for order in orders:
        order_payload = build_order_payload(order)
        woo_order_id = order_payload["woo_order_id"]

        if is_order_processed(woo_order_id):
            print("Skipping already processed Woo order:", woo_order_id)
            continue

        customer = get_or_create_customer(order_payload)
        sales_order = create_sales_order(order_payload, customer)

        mark_order_processed(
            woo_order_id,
            sales_order["name"],
            order_payload["status"]
        )

        print("Sales Order created:", sales_order["name"])


def sync_products_light():
    sync_products(limit=50, start=0)


def run_full_sync():
    print("Starting full sync...")

    print("\nSyncing products...")
    sync_products_light()

    print("\nSyncing orders...")
    sync_orders(limit=20)

    print("\nFull sync finished.")


def run_forever():
    print("Starting automatic sync service...")

    last_product_sync = 0

    while True:
        now = time.time()

        try:
            print("\nChecking WooCommerce orders...")
            sync_orders(limit=20)

            if now - last_product_sync >= PRODUCT_SYNC_SECONDS:
                print("\nSyncing ERP products to WooCommerce...")
                sync_products_light()
                last_product_sync = now

        except Exception as error:
            print("ERROR in sync loop:")
            print(error)

        print(f"Waiting {ORDER_SYNC_SECONDS} seconds...")
        time.sleep(ORDER_SYNC_SECONDS)


def sync_one_order_by_id(order_id):
    order = get_order(order_id)
    order_payload = build_order_payload(order)

    woo_order_id = order_payload["woo_order_id"]
    woo_status = order_payload["status"]

    processed_order = get_processed_order(woo_order_id)

    if processed_order:
        old_status = processed_order.get("status")
        sales_order_name = processed_order.get("sales_order")

        if old_status != woo_status:
            print(
                f"Woo order {woo_order_id} status changed: "
                f"{old_status} -> {woo_status}"
            )

            add_sales_order_comment(
                sales_order_name,
                f"WooCommerce order status changed from {old_status} to {woo_status}"
            )

            if woo_status == "cancelled":
                add_sales_order_comment(
                    sales_order_name,
                    "WooCommerce order was cancelled, but ERP Sales Order could not be auto-cancelled. Please cancel/release stock reservation manually."
                    )

            mark_order_processed(
                woo_order_id,
                sales_order_name,
                woo_status
            )

            return {
                "status": "updated",
                "woo_order_id": woo_order_id,
                "old_status": old_status,
                "new_status": woo_status,
                "sales_order": sales_order_name
            }

        print("Skipping already processed Woo order:", woo_order_id)

        return {
            "status": "skipped",
            "woo_order_id": woo_order_id
        }

    customer = get_or_create_customer(order_payload)

    sales_order = create_sales_order(
        order_payload,
        customer
    )

    submit_sales_order(
        sales_order["name"]
    )

    mark_order_processed(
        woo_order_id,
        sales_order["name"],
        woo_status
    )

    print(
        "Sales Order created from webhook:",
        sales_order["name"]
    )

    return {
        "status": "created",
        "woo_order_id": woo_order_id,
        "sales_order": sales_order["name"]
    }