"""
Migration: Purchase Order Returns
Run: python migrations/migrate_po_returns.py

Creates:
  1. purchase_order_returns       — return requests against a purchase order
  2. purchase_order_return_items  — line items per return (product, qty, condition)

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
        "CREATE TABLE purchase_order_returns",
        """
        CREATE TABLE IF NOT EXISTS purchase_order_returns (
            id              SERIAL PRIMARY KEY,
            return_number   VARCHAR(80)    UNIQUE NOT NULL,
            po_id           INTEGER        NOT NULL REFERENCES purchase_orders(id) ON DELETE CASCADE,
            warehouse_id    VARCHAR(50)    REFERENCES sites(site_id),
            status          VARCHAR(20)    NOT NULL DEFAULT 'Pending',
            total_credit    NUMERIC(12,2)  NOT NULL DEFAULT 0,
            notes           TEXT,
            created_by      INTEGER        REFERENCES users(id),
            processed_by    INTEGER        REFERENCES users(id),
            processed_at    TIMESTAMP,
            created_at      TIMESTAMP      NOT NULL DEFAULT NOW()
        )
        """,
    ),
    (
        "INDEX purchase_order_returns.po_id",
        """
        CREATE INDEX IF NOT EXISTS ix_purchase_order_returns_po_id
        ON purchase_order_returns (po_id)
        """,
    ),
    (
        "INDEX purchase_order_returns.status",
        """
        CREATE INDEX IF NOT EXISTS ix_purchase_order_returns_status
        ON purchase_order_returns (status)
        """,
    ),
    (
        "CREATE TABLE purchase_order_return_items",
        """
        CREATE TABLE IF NOT EXISTS purchase_order_return_items (
            id          SERIAL PRIMARY KEY,
            return_id   INTEGER        NOT NULL REFERENCES purchase_order_returns(id) ON DELETE CASCADE,
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
        "INDEX purchase_order_return_items.return_id",
        """
        CREATE INDEX IF NOT EXISTS ix_po_return_items_return_id
        ON purchase_order_return_items (return_id)
        """,
    ),
]


def run():
    print("Running migration: Purchase Order Returns\n")
    with engine.connect() as conn:
        for label, sql in MIGRATIONS:
            try:
                conn.execute(text(sql))
                conn.commit()
                print(f"  ✅  {label}")
            except Exception as e:
                print(f"  ❌  {label} — {e}")

    print("\nMigration complete.")
    print("Next: register bp_po_returns blueprint and restart Flask — PO returns will be live.")


if __name__ == "__main__":
    run()
