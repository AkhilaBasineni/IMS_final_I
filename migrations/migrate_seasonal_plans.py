"""
migrate_seasonal_plans.py
=========================
Adds enhanced columns to the existing seasonal_plans table:
  - notes          TEXT
  - created_by     INTEGER (FK → users.id)
  - created_at     TIMESTAMP
  - updated_at     TIMESTAMP
  - unique constraint (month, site_id, product_category)
  - changes seasonal_adjustments precision from NUMERIC(5,2) → NUMERIC(5,3)

Run once:
    cd /path/to/IMS_C_R
    python migrations/migrate_seasonal_plans.py
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.database import db
from sqlalchemy import text

app = create_app()

MIGRATIONS = [
    # Add notes column
    """ALTER TABLE seasonal_plans ADD COLUMN IF NOT EXISTS notes TEXT;""",

    # Add created_by FK
    """ALTER TABLE seasonal_plans ADD COLUMN IF NOT EXISTS created_by INTEGER
       REFERENCES users(id) ON DELETE SET NULL;""",

    # Add timestamps
    """ALTER TABLE seasonal_plans ADD COLUMN IF NOT EXISTS created_at TIMESTAMP
       DEFAULT NOW();""",
    """ALTER TABLE seasonal_plans ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP
       DEFAULT NOW();""",

    # Widen seasonal_adjustments to NUMERIC(5,3) to store 0.153 etc.
    """ALTER TABLE seasonal_plans
       ALTER COLUMN seasonal_adjustments TYPE NUMERIC(5,3)
       USING seasonal_adjustments::NUMERIC(5,3);""",

    # Set default for actual_sales if null
    """ALTER TABLE seasonal_plans ALTER COLUMN actual_sales SET DEFAULT 0;""",

    # Add unique constraint (idempotent via IF NOT EXISTS workaround)
    """DO $$ BEGIN
         IF NOT EXISTS (
           SELECT 1 FROM pg_constraint
           WHERE conname = '_seasonal_plan_uc'
         ) THEN
           ALTER TABLE seasonal_plans
             ADD CONSTRAINT _seasonal_plan_uc
             UNIQUE (month, site_id, product_category);
         END IF;
       END $$;""",
]

with app.app_context():
    with db.engine.connect() as conn:
        for sql in MIGRATIONS:
            try:
                conn.execute(text(sql))
                conn.commit()
                print(f"✓  {sql[:70].strip()}…")
            except Exception as e:
                print(f"⚠  Skipped (already applied?): {e}")
                conn.rollback()

    print("\n✅ seasonal_plans migration complete.")
