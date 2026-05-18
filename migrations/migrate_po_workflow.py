"""
Migration: Purchase Order Workflow Enhancement
Run: python migrate_po_workflow.py

Changes:
  1. suppliers.contact_email   — new nullable VARCHAR(255) column
  2. purchase_orders.supplier_id — change from VARCHAR to INTEGER, add FK to suppliers
"""

import os
from dotenv import load_dotenv
load_dotenv()

from sqlalchemy import create_engine, text

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set in environment / .env")

engine = create_engine(DATABASE_URL)

MIGRATIONS = [
    # 1. Add contact_email to suppliers (safe — idempotent via IF NOT EXISTS trick)
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='suppliers' AND column_name='contact_email'
        ) THEN
            ALTER TABLE suppliers ADD COLUMN contact_email VARCHAR(255);
        END IF;
    END$$;
    """,

    # 2a. Add new integer column supplier_int_id to purchase_orders
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='purchase_orders' AND column_name='supplier_int_id'
        ) THEN
            ALTER TABLE purchase_orders ADD COLUMN supplier_int_id INTEGER REFERENCES suppliers(supplier_id);
        END IF;
    END$$;
    """,

    # 2b. Populate supplier_int_id by matching existing string supplier_id names
    """
    UPDATE purchase_orders po
    SET supplier_int_id = s.supplier_id
    FROM suppliers s
    WHERE LOWER(TRIM(po.supplier_id::text)) = LOWER(TRIM(s.supplier_name))
      AND po.supplier_int_id IS NULL;
    """,

    # 2c. Rename columns: old supplier_id -> supplier_name_legacy, new -> supplier_id
    """
    DO $$
    BEGIN
        -- Only rename if supplier_int_id still exists (not yet renamed)
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name='purchase_orders' AND column_name='supplier_int_id'
        ) THEN
            -- Back up old text column
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name='purchase_orders' AND column_name='supplier_name_legacy'
            ) THEN
                ALTER TABLE purchase_orders RENAME COLUMN supplier_id TO supplier_name_legacy;
            END IF;
            -- Promote int column to supplier_id
            ALTER TABLE purchase_orders RENAME COLUMN supplier_int_id TO supplier_id;
        END IF;
    END$$;
    """,
]

def run():
    with engine.connect() as conn:
        for i, sql in enumerate(MIGRATIONS, 1):
            print(f"Running migration step {i}...")
            conn.execute(text(sql))
            conn.commit()
            print(f"  Step {i} OK")
    print("\n✅ All migrations complete.")
    print("   - suppliers.contact_email added")
    print("   - purchase_orders.supplier_id is now an INTEGER FK to suppliers")
    print("   - Old string values backed up to supplier_name_legacy")

if __name__ == '__main__':
    run()
