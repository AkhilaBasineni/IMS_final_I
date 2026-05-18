"""
Migration: Sales Order Module Enhancements
Run: python migrations/migrate_so_enhancements.py

Adds:
  1. customers.name           — VARCHAR(150) nullable
  2. customers.email          — VARCHAR(255) nullable
  3. customers.phone          — VARCHAR(30)  nullable
  4. sales_orders.confirmed_by — INTEGER FK → users.id, nullable
  5. sales_orders.notes        — TEXT nullable
  6. sales_orders.email_sent   — BOOLEAN default false
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
    # Customers
    ("customers.name",
     "ALTER TABLE customers ADD COLUMN IF NOT EXISTS name VARCHAR(150)"),
    ("customers.email",
     "ALTER TABLE customers ADD COLUMN IF NOT EXISTS email VARCHAR(255)"),
    ("customers.phone",
     "ALTER TABLE customers ADD COLUMN IF NOT EXISTS phone VARCHAR(30)"),
    # Sales Orders
    ("sales_orders.confirmed_by",
     "ALTER TABLE sales_orders ADD COLUMN IF NOT EXISTS confirmed_by INTEGER REFERENCES users(id)"),
    ("sales_orders.notes",
     "ALTER TABLE sales_orders ADD COLUMN IF NOT EXISTS notes TEXT"),
    ("sales_orders.email_sent",
     "ALTER TABLE sales_orders ADD COLUMN IF NOT EXISTS email_sent BOOLEAN DEFAULT FALSE"),
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
