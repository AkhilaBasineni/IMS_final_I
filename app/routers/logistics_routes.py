import csv
import os
from flask import Blueprint, jsonify, request
from app.auth import token_required, log_audit
from app.models import Site, State, SalesOrder, Customer, Inventory, StockLevel, StockMovement, db
from datetime import datetime, timezone, timezone

bp = Blueprint('logistics', __name__, url_prefix='/api')

# Paths to the CSV files (relative to project root)
_CSV_PATH      = os.path.join(os.path.dirname(__file__), '../../data/Logistics_Data.csv')
_SITES_CSV     = os.path.join(os.path.dirname(__file__), '../../data/Site_Details.csv')


def _get_site_ids_for_state(state_id):
    """
    Return a set of Site IDs that belong to the given state_id (int).
    Looks up via the DB (Site table) which is the authoritative source.
    Falls back to Site_Details.csv if no DB rows are found.
    """
    # Primary: use DB
    sites = Site.query.filter_by(state_id=state_id).all()
    if sites:
        return {s.site_id for s in sites}

    # Fallback: read from CSV using state name
    state = State.query.get(state_id)
    if not state:
        return set()
    state_name = state.state_name.strip().lower()

    site_ids = set()
    csv_path = os.path.abspath(_SITES_CSV)
    if os.path.exists(csv_path):
        with open(csv_path, newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                if row.get('State', '').strip().lower() == state_name:
                    sid = row.get('Site ID', '').strip()
                    if sid:
                        site_ids.add(sid)
    return site_ids


def _load_logistics(allowed_site_ids=None):
    """
    Load and merge logistics records from BOTH Logistics_Data.csv and the
    Logistics DB table, deduplicating by shipment_id (DB takes priority for
    status updates written via the status endpoint).
    If allowed_site_ids is a set, only records whose Site ID is in that
    set are returned.  Pass None to return all records (Admin behaviour).
    """
    seen = {}   # dedup_key -> record dict (DB rows take priority)
    # Track which base shipment IDs came from DB so CSV duplicates are skipped.
    # DB rows use composite IDs like SHP-SO123-PRD001; the CSV stores the base
    # SHP-SO123. We normalise to a (base_shipment_id, product_id) key so the
    # same physical item is never counted twice.
    db_base_keys = set()   # set of (base_shipment_id, product_id)

    # ── 1. Load from DB table (authoritative for records created on dispatch) ──
    try:
        from app.models import Logistics as LogisticsModel
        db_rows = LogisticsModel.query.all()
        for row in db_rows:
            site_id = (row.site_id or '').strip()
            if allowed_site_ids is not None and site_id not in allowed_site_ids:
                continue
            ship_date = row.shipment_date.strftime('%Y-%m-%d') if row.shipment_date else ''
            prod_id   = (row.product_id or '').strip()
            shp_id    = row.shipment_id  # may be composite, e.g. SHP-SO123-PRD001
            seen[shp_id] = {
                'shipment_id':         shp_id,
                'site_id':             site_id,
                'product_id':          prod_id,
                'shipment_date':       ship_date,
                'quantity':            row.quantity or 0,
                'delivery_status':     (row.delivery_status or '').strip(),
                'transportation_type': (row.transportation_type or '').strip(),
            }
            # Record the base ID (strip trailing -<product_id> suffix if present)
            base_id = shp_id[:-len(f'-{prod_id}')].rstrip('-') if prod_id and shp_id.endswith(f'-{prod_id}') else shp_id
            db_base_keys.add((base_id, prod_id))
    except Exception as e:
        print(f"[LOGISTICS] DB load error: {e}")

    # ── 2. Load from CSV (seed data + any rows not yet in DB) ──────────────────
    csv_path = os.path.abspath(_CSV_PATH)
    if os.path.exists(csv_path):
        try:
            with open(csv_path, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    site_id = row.get('Site ID', '').strip()
                    if allowed_site_ids is not None and site_id not in allowed_site_ids:
                        continue
                    shp_id  = row.get('Shipment ID', '').strip()
                    prod_id = row.get('Product ID', '').strip()
                    if not shp_id:
                        continue
                    # Skip if DB already has this (base_id, product_id) pair
                    if (shp_id, prod_id) in db_base_keys:
                        continue
                    if shp_id not in seen:   # exact dedup for pure-CSV rows
                        seen[shp_id] = {
                            'shipment_id':         shp_id,
                            'site_id':             site_id,
                            'product_id':          prod_id,
                            'shipment_date':       row.get('Shipment Date', '').strip(),
                            'quantity':            int(row.get('Quantity', 0) or 0),
                            'delivery_status':     row.get('Delivery Status', '').strip(),
                            'transportation_type': row.get('Transportation Type', '').strip(),
                        }
        except Exception as e:
            print(f"[LOGISTICS] CSV load error: {e}")

    return list(seen.values())


def _enrich_with_so(records):
    """
    Enrich each logistics record with SalesOrder info.
    Priority 1: shipment_id matches SO tracking_number or so_number.
    Priority 2: product_id + site_id match an SO item.
    Priority 3: site_id fallback to most recent SO for that site.
    Also attaches revenue/units_sold from Sale table via product+site.
    """
    from app.models import Sale as SaleModel
    # Load ALL sales orders regardless of status
    all_sos = SalesOrder.query.all()

    so_by_tracking = {}
    so_by_number   = {}
    site_so_map    = {}
    product_site_so_map = {}

    for so in all_sos:
        if so.tracking_number:
            so_by_tracking[so.tracking_number.strip()] = so
        if so.so_number:
            so_by_number[so.so_number.strip()] = so
        site_so_map.setdefault(so.warehouse_id, []).append(so)
        for item in (so.items or []):
            key = (item.product_id, so.warehouse_id)
            product_site_so_map.setdefault(key, []).append(so)

    customer_map = {c.customer_id: c.name or c.customer_id for c in Customer.query.all()}

    # Build sale revenue/units lookup by (product_id, site_id)
    sale_lookup = {}
    for s in SaleModel.query.all():
        key = (s.product_id, s.site_id)
        if key not in sale_lookup:
            sale_lookup[key] = {'revenue': 0.0, 'units_sold': 0}
        sale_lookup[key]['revenue']    += float(s.revenue or 0)
        sale_lookup[key]['units_sold'] += int(s.units_sold or 0)

    def _match_so(r):
        shp_id  = r.get('shipment_id', '').strip()
        site_id = r.get('site_id', '').strip()
        prod_id = r.get('product_id', '').strip()
        # Priority 1: tracking number or SO number exact match
        if shp_id in so_by_tracking:
            return so_by_tracking[shp_id]
        if shp_id in so_by_number:
            return so_by_number[shp_id]
        # Priority 1b: shipment_id contains SO number
        for so_num, so in so_by_number.items():
            if so_num and so_num in shp_id:
                return so
        # Priority 2: product+site match
        candidates = product_site_so_map.get((prod_id, site_id), [])
        if candidates:
            return candidates[0]
        # Priority 3: site fallback - pick most recent
        sos = site_so_map.get(site_id, [])
        if sos:
            return sorted(sos, key=lambda x: x.order_date or x.created_at, reverse=True)[0]
        return None

    enriched = []
    for r in records:
        so = _match_so(r)
        r  = dict(r)
        if so:
            r['so_number']     = so.so_number or ''
            r['customer_id']   = so.customer_id or ''
            r['customer_name'] = customer_map.get(so.customer_id, so.customer_id or '—') \
                                 if so.customer_id else '—'
        else:
            r['so_number']     = ''
            r['customer_id']   = ''
            r['customer_name'] = '—'
        # Attach sales data
        sale_key = (r.get('product_id', ''), r.get('site_id', ''))
        sale_info = sale_lookup.get(sale_key, {})
        r['revenue']    = sale_info.get('revenue', 0.0)
        r['units_sold'] = sale_info.get('units_sold', 0)
        enriched.append(r)
    return enriched


@bp.route('/logistics', methods=['GET'])
@token_required
def get_logistics(current_user):
    """
    Return logistics shipment records.
    - Admin / users with state_id == 'ALL' → all shipments.
    - Manager / Analyst with a specific state_id → only shipments
      belonging to sites in their state.
    Supports optional ?customer_id= filter.
    """
    try:
        state_id    = current_user.state_id  # may be None, 'ALL', or a numeric string
        customer_f  = request.args.get('customer_id', '').strip()

        # Determine which site IDs are visible to this user
        if state_id and state_id != 'ALL':
            try:
                allowed_site_ids = _get_site_ids_for_state(int(state_id))
            except (ValueError, TypeError):
                allowed_site_ids = set()
        else:
            allowed_site_ids = None  # unrestricted

        data = _load_logistics(allowed_site_ids)
        data = _enrich_with_so(data)

        # Filter by customer if requested
        if customer_f:
            data = [r for r in data if r.get('customer_id') == customer_f]

        return jsonify({'success': True, 'data': data, 'total': len(data)})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/logistics/<shipment_id>/status', methods=['PUT'])
@token_required
def update_logistics_status(current_user, shipment_id):
    """
    Update the delivery_status of a specific shipment in the CSV.
    Allowed statuses: Delivered, Cancelled, Delayed, In Transit
    Analysts are view-only and cannot update status.
    """
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403

    ALLOWED_STATUSES = {'Delivered', 'Cancelled', 'Delayed', 'In Transit'}
    try:
        body = request.get_json(force=True) or {}
        new_status = (body.get('delivery_status') or '').strip()

        if new_status not in ALLOWED_STATUSES:
            return jsonify({'success': False, 'message': f'Invalid status. Must be one of: {", ".join(sorted(ALLOWED_STATUSES))}'}), 400

        # ── Block editing if already Delivered or Cancelled ───────────────
        from app.models import Logistics as LogisticsModel
        existing = LogisticsModel.query.filter_by(shipment_id=shipment_id).first()
        if existing and existing.delivery_status in ('Delivered', 'Cancelled'):
            return jsonify({'success': False,
                'message': f'Cannot edit a shipment that is already {existing.delivery_status}.'}), 400
        db_updated = False
        db_updated_row = None
        try:
            # Exact match first; then prefix match for composite IDs like
            # SHP-20260421-8126-SO-20260419232165-PRD10096
            db_row = LogisticsModel.query.filter_by(shipment_id=shipment_id).first()
            if db_row is None:
                db_row = LogisticsModel.query.filter(
                    LogisticsModel.shipment_id.like(f'{shipment_id}%')
                ).first()
            if db_row:
                db_updated_row = {
                    'Product ID':  db_row.product_id or '',
                    'Site ID':     db_row.site_id or '',
                    'Quantity':    db_row.quantity or 0,
                    '_old_status': db_row.delivery_status or '',
                }
                db_row.delivery_status = new_status
                db.session.commit()
                db_updated = True
        except Exception as db_err:
            db.session.rollback()
            print(f"[LOGISTICS] DB update error: {db_err}")

        # ── 2. Update in CSV — ONLY for records NOT already handled by DB ──
        # DB-created shipments use composite IDs (SHP-SO123-PRD001). The CSV
        # uses shorter base IDs. If we also update+deduct from the CSV side
        # for a DB record, inventory gets deducted twice.  So we skip CSV
        # processing entirely when the DB already owns this shipment.
        csv_path = os.path.abspath(_CSV_PATH)
        updated = False
        updated_row = None
        if not db_updated and os.path.exists(csv_path):
            rows = []
            with open(csv_path, newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    csv_shp = row.get('Shipment ID', '').strip()
                    # Exact match only — no substring/partial match to avoid
                    # accidentally updating unrelated seed-data rows.
                    if csv_shp == shipment_id:
                        updated_row = dict(row)
                        updated_row['_old_status'] = row.get('Delivery Status', '').strip()
                        row['Delivery Status'] = new_status
                        updated = True
                    rows.append(row)

            if updated:
                with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(rows)

        # ── 3. If neither source has the record, return 404 ────────────────
        if not db_updated and not updated:
            return jsonify({'success': False, 'message': f'Shipment {shipment_id} not found'}), 404

        # DB row is the single authoritative source for inventory deduction.
        # Never merge both — pick DB first, then CSV (never both).
        if db_updated_row is not None:
            updated_row = db_updated_row

        # ── Deduct inventory when status changes to Delivered ──────────────
        inventory_msg = None
        if new_status == 'Delivered' and updated_row:
            product_id = updated_row.get('Product ID', '').strip()
            site_id    = updated_row.get('Site ID', '').strip()
            qty        = int(updated_row.get('Quantity', 0) or 0)
            old_status = updated_row.get('_old_status', '')

            # Only deduct if this is a fresh transition to Delivered
            if old_status != 'Delivered' and product_id and site_id and qty > 0:
                inv = Inventory.query.filter_by(
                    product_id=product_id, site_id=site_id
                ).first()

                if inv:
                    before = inv.ending_inventory or 0
                    inv.ending_inventory = max(0, before - qty)
                    inv.stockout_flag    = 'Yes' if inv.ending_inventory <= 0 else 'No'

                    # Keep StockLevel in sync
                    stock = StockLevel.query.filter_by(
                        product_id=product_id, site_id=site_id
                    ).first()
                    if stock:
                        stock.current_quantity = inv.ending_inventory
                        stock.last_updated     = datetime.now(timezone.utc)

                    # Audit movement record
                    movement = StockMovement(
                        product_id=product_id,
                        site_id=site_id,
                        quantity=-qty,
                        movement_type='LOGISTICS_DELIVERED',
                        reference_id=shipment_id,
                        notes=f'Shipment {shipment_id} marked Delivered — {qty} units deducted from inventory',
                        created_by=current_user.username,
                        created_at=datetime.now(timezone.utc),
                    )
                    db.session.add(movement)
                    db.session.commit()
                    inventory_msg = (
                        f'Inventory for product {product_id} at site {site_id} '
                        f'reduced by {qty} (from {before} to {inv.ending_inventory}).'
                    )
                else:
                    inventory_msg = (
                        f'No inventory record found for product {product_id} '
                        f'at site {site_id}; inventory not updated.'
                    )
        # ──────────────────────────────────────────────────────────────────

        response = {
            'success': True,
            'message': f'Status updated to {new_status}',
            'shipment_id': shipment_id,
            'delivery_status': new_status,
        }
        log_audit(current_user, 'UPDATE_STATUS', 'Logistics', shipment_id, {'new_status': new_status})
        if inventory_msg:
            response['inventory_update'] = inventory_msg
        return jsonify(response)
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/logistics/customers', methods=['GET'])
@token_required
def get_logistics_customers(current_user):
    """
    Return list of customers who have shipped/confirmed sales orders
    — used to populate the customer dropdown on the logistics page.
    """
    try:
        orders = SalesOrder.query.filter(
            SalesOrder.status.in_(['Shipped', 'Confirmed']),
            SalesOrder.customer_id.isnot(None)
        ).with_entities(SalesOrder.customer_id).distinct().all()

        customer_ids = [r.customer_id for r in orders]
        customers = Customer.query.filter(Customer.customer_id.in_(customer_ids)).all()
        return jsonify({'success': True, 'data': [
            {'customer_id': c.customer_id, 'name': c.name or c.customer_id}
            for c in customers
        ]})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500
