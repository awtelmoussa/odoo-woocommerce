import json
import os


PROCESSED_ORDERS_FILE = "processed_orders.json"


def load_processed_orders():
    if not os.path.exists(PROCESSED_ORDERS_FILE):
        return {}

    with open(PROCESSED_ORDERS_FILE, "r") as file:
        return json.load(file)


def save_processed_orders(processed_orders):
    with open(PROCESSED_ORDERS_FILE, "w") as file:
        json.dump(processed_orders, file, indent=4)


def is_order_processed(woo_order_id):
    processed_orders = load_processed_orders()

    return str(woo_order_id) in processed_orders


def mark_order_processed(woo_order_id, sales_order_name, status=None):
    processed_orders = load_processed_orders()

    processed_orders[str(woo_order_id)] = {
        "sales_order": sales_order_name,
        "status": status
    }

    save_processed_orders(processed_orders)

def get_processed_order(woo_order_id):
    processed_orders = load_processed_orders()

    return processed_orders.get(str(woo_order_id))    