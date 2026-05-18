"""Add transport column to sales_orders table

Run this script once against your database:
    python migrations/add_transport_to_sales_orders.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app
from app.database import db
from sqlalchemy import text, inspect

app = create_app()
with app.app_context():
    inspector = inspect(db.engine)
    columns = [col['name'] for col in inspector.get_columns('sales_orders')]

    if 'transport' in columns:
        print("✓ Column 'transport' already exists — nothing to do.")
    else:
        try:
            with db.engine.connect() as conn:
                conn.execute(text("ALTER TABLE sales_orders ADD COLUMN transport VARCHAR(20);"))
                conn.commit()
            print("✓ Column 'transport' successfully added to sales_orders.")
        except Exception as e:
            print(f"✗ Error: {e}")
