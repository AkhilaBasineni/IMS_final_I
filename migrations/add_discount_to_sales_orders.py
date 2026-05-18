"""
Migration: Add discount column to sales_orders
Run: python migrations/add_discount_to_sales_orders.py

Adds:
  1. sales_orders.discount — NUMERIC(10,2) default 0
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
    ("sales_orders.discount",
     "ALTER TABLE sales_orders ADD COLUMN IF NOT EXISTS discount NUMERIC(10,2) DEFAULT 0"),
]

def run():
    with engine.connect() as conn:
        for label, sql in MIGRATIONS:
            try:
                conn.execute(text(sql))
                conn.commit()
                print(f"  ✅  {label}")
            except Exception as e:
                print(f"  ⚠️  {label} — {e}")
    print("\nMigration complete.")

if __name__ == "__main__":
    run()
