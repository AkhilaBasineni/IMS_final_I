"""
Migration: Sales Order Returns
Run: python migrations/migrate_so_returns.py

Creates:
  1. sales_order_returns       — return requests against a sales order
  2. sales_order_return_items  — line items per return (product, qty, condition)

Idempotent — safe to re-run (uses IF NOT EXISTS throughout).
"""

import os
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set in .env")

engine = create_engine(DATABASE_URL)

MIGRATIONS = [
    (
        "CREATE TABLE sales_order_returns",
        """
        CREATE TABLE IF NOT EXISTS sales_order_returns (
            id              SERIAL PRIMARY KEY,
            return_number   VARCHAR(80)    UNIQUE NOT NULL,
            so_id           INTEGER        NOT NULL REFERENCES sales_orders(id) ON DELETE CASCADE,
            warehouse_id    VARCHAR(50)    REFERENCES sites(site_id),
            status          VARCHAR(20)    NOT NULL DEFAULT 'Pending',
            total_refund    NUMERIC(12,2)  NOT NULL DEFAULT 0,
            notes           TEXT,
            created_by      INTEGER        REFERENCES users(id),
            processed_by    INTEGER        REFERENCES users(id),
            processed_at    TIMESTAMP,
            created_at      TIMESTAMP      NOT NULL DEFAULT NOW()
        )
        """,
    ),
    (
        "INDEX sales_order_returns.so_id",
        """
        CREATE INDEX IF NOT EXISTS ix_sales_order_returns_so_id
        ON sales_order_returns (so_id)
        """,
    ),
    (
        "INDEX sales_order_returns.status",
        """
        CREATE INDEX IF NOT EXISTS ix_sales_order_returns_status
        ON sales_order_returns (status)
        """,
    ),
    (
        "CREATE TABLE sales_order_return_items",
        """
        CREATE TABLE IF NOT EXISTS sales_order_return_items (
            id          SERIAL PRIMARY KEY,
            return_id   INTEGER        NOT NULL REFERENCES sales_order_returns(id) ON DELETE CASCADE,
            product_id  VARCHAR(50)    REFERENCES products(product_id),
            return_qty  INTEGER        NOT NULL,
            condition   VARCHAR(20)    NOT NULL,
            unit_price  NUMERIC(10,2)  NOT NULL DEFAULT 0,
            line_total  NUMERIC(12,2)  NOT NULL DEFAULT 0,
            reason      VARCHAR(255)
        )
        """,
    ),
    (
        "INDEX sales_order_return_items.return_id",
        """
        CREATE INDEX IF NOT EXISTS ix_so_return_items_return_id
        ON sales_order_return_items (return_id)
        """,
    ),
]


def run():
    print("Running migration: Sales Order Returns\n")
    with engine.connect() as conn:
        for label, sql in MIGRATIONS:
            try:
                conn.execute(text(sql))
                conn.commit()
                print(f"  ✅  {label}")
            except Exception as e:
                print(f"  ❌  {label} — {e}")

    print("\nMigration complete.")
    print("Next: restart Flask — the /sales-order-returns page will be live.")


if __name__ == "__main__":
    run()
