"""
backfill_logistics.py
─────────────────────
One-time migration script: reads every row from Logistics_Data.csv
and inserts it into the `logistics` database table (if not already present).

Usage (run from the project root):
    python scripts/backfill_logistics.py

The script is safe to run multiple times — it skips rows whose
shipment_id already exists in the DB.
"""

import os
import sys
import csv
from datetime import datetime

# ── Make sure the app package is importable ───────────────────────────────────
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import create_app          # adjust if your factory is named differently
from app.models import Logistics, db

CSV_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'Logistics_Data.csv')


def parse_date(date_str):
    """Try common date formats; return None on failure."""
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%m/%d/%Y', '%d-%m-%Y'):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def backfill():
    app = create_app()
    with app.app_context():
        csv_path = os.path.abspath(CSV_PATH)
        if not os.path.exists(csv_path):
            print(f"[ERROR] CSV not found: {csv_path}")
            sys.exit(1)

        inserted = 0
        skipped  = 0
        errors   = 0

        with open(csv_path, newline='', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader, start=2):   # row 1 is header
                shipment_id = row.get('Shipment ID', '').strip()
                if not shipment_id:
                    print(f"  [WARN] Row {i}: empty Shipment ID — skipped")
                    skipped += 1
                    continue

                # Skip if already in DB
                if Logistics.query.filter_by(shipment_id=shipment_id).first():
                    skipped += 1
                    continue

                date_obj = parse_date(row.get('Shipment Date', ''))
                if date_obj is None:
                    print(f"  [WARN] Row {i}: cannot parse date '{row.get('Shipment Date')}' for {shipment_id}")

                try:
                    qty_raw = row.get('Quantity', '0').strip()
                    qty = int(qty_raw) if qty_raw.lstrip('-').isdigit() else 0
                except ValueError:
                    qty = 0

                entry = Logistics(
                    shipment_id       = shipment_id,
                    site_id           = row.get('Site ID', '').strip() or None,
                    product_id        = row.get('Product ID', '').strip() or None,
                    shipment_date     = date_obj,
                    quantity          = qty,
                    delivery_status   = row.get('Delivery Status', '').strip() or None,
                    transportation_type = row.get('Transportation Type', '').strip() or None,
                )
                db.session.add(entry)

                try:
                    db.session.flush()   # catch constraint errors early
                    inserted += 1
                except Exception as e:
                    db.session.rollback()
                    print(f"  [ERROR] Row {i} ({shipment_id}): {e}")
                    errors += 1

        db.session.commit()

        total = inserted + skipped + errors
        print("\n── Backfill complete ─────────────────────────────")
        print(f"   Rows in CSV  : {total}")
        print(f"   Inserted     : {inserted}")
        print(f"   Skipped (dup): {skipped}")
        print(f"   Errors       : {errors}")
        print("──────────────────────────────────────────────────\n")

        if inserted:
            print(f"✅  {inserted} new record(s) added to the logistics table.")
            print("   The home page 'Total Shipments' count will now reflect them.")
        else:
            print("ℹ️   Nothing new to insert — DB was already up to date.")


if __name__ == '__main__':
    backfill()
