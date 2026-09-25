"""
Main CLI entrypoint for Odoo <-> WooCommerce operations.

Usage:
  python main.py product --sku sf-5           # Sync single product by SKU
  python main.py product --id 123             # Sync single product by Odoo ID
  python main.py product --all --limit 50     # Bulk sync products
  python main.py order --id 8387              # Import single WooCommerce order
  python main.py stock --once                 # Run one stock sync cycle
  python main.py reconcile --hours 24         # Reconcile recent WooCommerce orders
  python main.py server                       # Start the FastAPI webhook server
"""

import argparse
import sys
import uvicorn

from order_sync import import_wc_order_to_odoo, reconcile_recent_orders
from product_sync import sync_one_product_by_sku, sync_one_product_by_odoo_id, sync_products
from stock_sync import run_stock_sync_once


def main():
    parser = argparse.ArgumentParser(description="Odoo <-> WooCommerce Integration CLI")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # 1. Product command
    prod_parser = subparsers.add_parser("product", help="Sync products from Odoo to WooCommerce")
    prod_parser.add_argument("--sku", type=str, help="Sync specific product by SKU")
    prod_parser.add_argument("--id", type=int, help="Sync specific product by Odoo ID")
    prod_parser.add_argument("--all", action="store_true", help="Sync multiple products")
    prod_parser.add_argument("--limit", type=int, default=50, help="Limit for bulk sync (default 50)")
    prod_parser.add_argument("--offset", type=int, default=0, help="Offset for bulk sync (default 0)")

    # 2. Order command
    order_parser = subparsers.add_parser("order", help="Import or update a WooCommerce order")
    order_parser.add_argument("--id", type=int, required=True, help="WooCommerce order ID")

    # 3. Stock command
    stock_parser = subparsers.add_parser("stock", help="Run inventory/stock sync")
    stock_parser.add_argument("--once", action="store_true", default=True, help="Run single cycle and exit")
    stock_parser.add_argument("--refresh-cache", action="store_true", help="Force refresh WooCommerce SKU map cache")

    # 4. Reconcile command
    rec_parser = subparsers.add_parser("reconcile", help="Reconcile recent WooCommerce orders")
    rec_parser.add_argument("--hours", type=int, default=24, help="Number of hours to look back (default 24)")

    # 5. Server command
    server_parser = subparsers.add_parser("server", help="Run the FastAPI webhook server")
    server_parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind (default 0.0.0.0)")
    server_parser.add_argument("--port", type=int, default=8000, help="Port to bind (default 8000)")
    server_parser.add_argument("--reload", action="store_true", help="Enable auto-reload for development")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "product":
        if args.sku:
            print(f"Syncing product with SKU: {args.sku}...")
            res = sync_one_product_by_sku(args.sku)
            print("Result:", res)
        elif args.id:
            print(f"Syncing product with Odoo ID: {args.id}...")
            res = sync_one_product_by_odoo_id(args.id)
            print("Result:", res)
        elif args.all:
            print(f"Bulk syncing Odoo products (limit={args.limit}, offset={args.offset})...")
            count = sync_products(limit=args.limit, offset=args.offset)
            print(f"Done. {count} products synced.")
        else:
            prod_parser.print_help()

    elif args.command == "order":
        print(f"Importing WooCommerce order #{args.id} into Odoo...")
        so_id = import_wc_order_to_odoo(args.id)
        if so_id:
            print(f"Successfully imported as Odoo SO id={so_id}")
        else:
            print("Order import was skipped or failed. Check logs.")

    elif args.command == "stock":
        print("Running stock synchronization...")
        updated = run_stock_sync_once(force_refresh_cache=args.refresh_cache)
        print(f"Stock sync finished. {updated} products updated.")

    elif args.command == "reconcile":
        print(f"Reconciling WooCommerce orders from the last {args.hours} hours...")
        summary = reconcile_recent_orders(hours=args.hours)
        print("Reconciliation summary:", summary)

    elif args.command == "server":
        print(f"Starting FastAPI Webhook Gateway on {args.host}:{args.port}...")
        uvicorn.run("app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()