import csv
import os
from flask import Blueprint, request, jsonify
from app.models import Inventory, Product, Site, State, Sale, StockLevel, StockMovement, SalesOrder, SalesOrderItem, SalesOrderReturn, SalesOrderReturnItem, db
from app.auth import token_required, role_required, log_audit
from app.dependencies import handle_exceptions
from sqlalchemy import or_, func
from datetime import datetime, timezone, timezone

bp = Blueprint('inventory', __name__, url_prefix='/api/inventory')

# Path to Sales_Data.csv (relative to this file)
_SALES_CSV_PATH = os.path.join(os.path.dirname(__file__), '../../data/Sales_Data.csv')


def _allowed_site_ids(current_user):
    """Admin → None (all). Manager/Analyst with ALL → None (all). Manager/Analyst with state → their sites only."""
    role = current_user.role.role_name
    if role == 'Admin':
        return None
    if role in ('Manager', 'Analyst'):
        state_id = current_user.state_id
        if not state_id or state_id == 'ALL':
            return None
        try:
            sites = Site.query.filter_by(state_id=int(state_id)).all()
            return [s.site_id for s in sites]
        except (ValueError, TypeError):
            return []
    return None


def _get_sales_for(site_id, product_id):
    """
    Return (units_sold, returns) for a (site, product) pair.

    Priority:
      1. Sale table — seeded historical data + records written by the ship hook.
         If ANY Sale rows exist for this site+product, trust them exclusively
         (the ship hook maintains them going forward).
      2. SalesOrderItem fallback — for orders shipped BEFORE the Sale-write hook
         was introduced (no Sale record exists yet).
      3. Sales_Data.csv — final fallback for seed data not imported into DB.
    """
    # ── 1. Sale table ────────────────────────────────────────────────────────
    sale_row = db.session.query(
        func.coalesce(func.sum(Sale.units_sold), 0).label('units_sold'),
        func.coalesce(func.sum(Sale.returns),    0).label('returns')
    ).filter(
        Sale.site_id    == site_id,
        Sale.product_id == product_id
    ).first()

    sale_units   = int(sale_row.units_sold or 0)
    sale_returns = int(sale_row.returns    or 0)

    # If Sale table has ANY data for this pair, use it — do NOT mix with other sources
    if sale_units > 0 or sale_returns > 0:
        return (sale_units, sale_returns)

    # ── 2. SalesOrderItem fallback (pre-hook shipped orders) ─────────────────
    so_row = db.session.query(
        func.coalesce(func.sum(SalesOrderItem.shipped_quantity), 0).label('shipped')
    ).join(
        SalesOrder, SalesOrderItem.so_id == SalesOrder.id
    ).filter(
        SalesOrder.warehouse_id == site_id,
        SalesOrderItem.product_id == product_id,
        SalesOrder.status.in_(['Shipped', 'Delivered']),
        SalesOrderItem.shipped_quantity > 0
    ).first()
    so_units = int(so_row.shipped or 0)

    if so_units > 0:
        return (so_units, 0)

    # ── 3. CSV fallback ──────────────────────────────────────────────────────
    csv_path = os.path.abspath(_SALES_CSV_PATH)
    if os.path.exists(csv_path):
        total_units = 0
        total_returns = 0
        try:
            with open(csv_path, newline='', encoding='utf-8') as f:
                for row in csv.DictReader(f):
                    if ((row.get('Site ID') or row.get('site_id') or '').strip() == site_id and
                            (row.get('Product ID') or row.get('product_id') or '').strip() == product_id):
                        total_units   += int(float(row.get('Units Sold') or row.get('units_sold') or 0))
                        total_returns += int(float(row.get('Returns')    or row.get('returns')    or 0))
        except Exception:
            pass
        if total_units > 0 or total_returns > 0:
            return (total_units, total_returns)

    return (0, 0)


def _format_item(inv, include_sales=False):
    product    = inv.product_rel
    site       = inv.site_rel
    state_name = None
    if site and site.state_id:
        state      = State.query.get(site.state_id)
        state_name = state.state_name if state else None

    data = {
        'id':                   inv.id,
        'site_id':              inv.site_id,
        'site_name':            site.site_name if site else 'Unknown',
        'state_name':           state_name,
        'product_id':           inv.product_id,
        'product_name':         product.product_name if product else 'Unknown',
        'category':             product.category if product else '',
        'subcategory':          product.subcategory if product else '',
        'beginning_inventory':  inv.beginning_inventory,
        'ending_inventory':     inv.ending_inventory,
        'replenishment':        inv.replenishment,
        'stockout_flag':        inv.stockout_flag,
        'created_at':           inv.created_at.isoformat() if inv.created_at else None,
    }

    if include_sales:
        units_sold, returns = _get_sales_for(inv.site_id, inv.product_id)
        data['units_sold'] = units_sold
        data['returns']    = returns
        # net_sold is finalised after SO returns are computed below
        data['net_sold']   = units_sold - returns

        # ── SO Returns breakdown ─────────────────────────────────────────────
        so_rets = (SalesOrderReturn.query
                   .filter_by(warehouse_id=inv.site_id)
                   .all())
        good_qty    = 0
        damaged_qty = 0
        pending_qty = 0
        approved_count = 0
        pending_count  = 0
        rejected_count = 0
        for ret in so_rets:
            for ri in ret.items:
                if ri.product_id != inv.product_id:
                    continue
                if ret.status == 'Approved':
                    approved_count += 1
                    if ri.condition == 'Good':
                        good_qty += ri.return_qty
                    else:
                        damaged_qty += ri.return_qty
                elif ret.status == 'Pending':
                    pending_count += 1
                    pending_qty   += ri.return_qty
                elif ret.status == 'Rejected':
                    rejected_count += 1

        data['so_returns'] = {
            'approved': approved_count,
            'pending':  pending_count,
            'rejected': rejected_count,
            'good_qty':     good_qty,
            'damaged_qty':  damaged_qty,
            'pending_qty':  pending_qty,
        }

        # Recalculate net_sold: subtract approved good-condition SO returns
        data['net_sold'] = max(units_sold - returns - good_qty, 0)

        # Option A: always compute ending_inventory live so it never goes stale
        data['ending_inventory'] = max(
            (inv.beginning_inventory or 0) + (inv.replenishment or 0) - data['net_sold'], 0
        )

    return data


def _sync_stock_level(inv, current_user=None, movement_type='INVENTORY_UPDATE', notes=None):
    """
    Keep StockLevel.current_quantity in sync with Inventory.ending_inventory.
    Creates a StockMovement audit record for every change.
    """
    stock = StockLevel.query.filter_by(
        product_id=inv.product_id,
        site_id=inv.site_id
    ).first()

    new_qty = inv.ending_inventory or 0

    if stock is None:
        # First time — create the StockLevel row
        stock = StockLevel(
            product_id=inv.product_id,
            site_id=inv.site_id,
            current_quantity=new_qty,
            last_updated=datetime.now(timezone.utc),
        )
        db.session.add(stock)
        delta = new_qty
    else:
        delta = new_qty - stock.current_quantity
        stock.current_quantity = new_qty
        stock.last_updated = datetime.now(timezone.utc)

    # Record the movement so there is a full audit trail
    movement = StockMovement(
        product_id=inv.product_id,
        site_id=inv.site_id,
        quantity=delta,
        movement_type=movement_type,
        reference_id=str(inv.id),
        notes=notes or f'Auto-synced from inventory record #{inv.id}',
        created_by=current_user.username if current_user else 'system',
        created_at=datetime.now(timezone.utc),
    )
    db.session.add(movement)
    return stock


@bp.route('', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_inventory(current_user):
    page        = request.args.get('page', 1, type=int)
    per_page    = request.args.get('per_page', 15, type=int)
    search      = request.args.get('search', '').strip()
    stockout    = request.args.get('stockout', '').strip()
    site_filter = request.args.get('site_id', '').strip()

    query = (Inventory.query
             .join(Product, Inventory.product_id == Product.product_id)
             .join(Site,    Inventory.site_id    == Site.site_id))

    allowed = _allowed_site_ids(current_user)
    if allowed is not None:
        query = query.filter(Inventory.site_id.in_(allowed))

    if site_filter:
        query = query.filter(Inventory.site_id == site_filter)

    if search:
        query = query.filter(or_(
            Product.product_name.ilike(f'%{search}%'),
            Product.product_id.ilike(f'%{search}%'),
            Product.category.ilike(f'%{search}%'),
            Site.site_name.ilike(f'%{search}%'),
            Inventory.site_id.ilike(f'%{search}%'),
        ))

    if stockout:
        query = query.filter(Inventory.stockout_flag == stockout)

    pagination = query.order_by(Inventory.id.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return jsonify({'success': True, 'data': {
        'items':        [_format_item(i, include_sales=True) for i in pagination.items],
        'total':        pagination.total,
        'pages':        pagination.pages,
        'current_page': page,
    }})


@bp.route('/<int:inv_id>', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_one(current_user, inv_id):
    inv = Inventory.query.get(inv_id)
    if not inv:
        return jsonify({'success': False, 'message': 'Not found'}), 404

    allowed = _allowed_site_ids(current_user)
    if allowed is not None and inv.site_id not in allowed:
        return jsonify({'success': False, 'message': 'Forbidden'}), 403

    # include_sales=True → units_sold & returns from Sales table
    return jsonify({'success': True, 'data': _format_item(inv, include_sales=True)})


@bp.route('', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def create_inventory(current_user):
    data = request.json

    site_id    = data.get('site_id', '').strip()
    product_id = data.get('product_id', '').strip()

    if not site_id or not product_id:
        return jsonify({'success': False, 'message': 'site_id and product_id are required'}), 400

    allowed = _allowed_site_ids(current_user)
    if allowed is not None and site_id not in allowed:
        return jsonify({'success': False, 'message': 'Forbidden: site not in your state'}), 403

    existing = Inventory.query.filter_by(site_id=site_id, product_id=product_id).first()
    if existing:
        return jsonify({'success': False, 'message': 'Inventory record already exists for this site + product'}), 400

    replenishment = int(data.get('replenishment', 0))
    inv = Inventory(
        site_id              = site_id,
        product_id           = product_id,
        beginning_inventory  = 0,                # Always 0 for new records
        replenishment        = replenishment,
        ending_inventory     = replenishment,     # ending = 0 + replenishment
        stockout_flag        = 'Yes' if replenishment <= 0 else 'No',
    )
    db.session.add(inv)
    db.session.flush()   # get inv.id before commit

    # ── Auto-update StockLevel ──────────────────────────────────────────────
    _sync_stock_level(
        inv,
        current_user=current_user,
        movement_type='INVENTORY_CREATE',
        notes=f'Initial stock from new inventory record',
    )
    # ───────────────────────────────────────────────────────────────────────

    db.session.commit()
    log_audit(current_user, 'CREATE', 'Inventory', inv.id)
    return jsonify({'success': True, 'message': 'Inventory record created', 'data': _format_item(inv)}), 201


@bp.route('/<int:inv_id>', methods=['PUT'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def update_inventory(current_user, inv_id):
    inv = Inventory.query.get(inv_id)
    if not inv:
        return jsonify({'success': False, 'message': 'Not found'}), 404

    allowed = _allowed_site_ids(current_user)
    if allowed is not None and inv.site_id not in allowed:
        return jsonify({'success': False, 'message': 'Forbidden'}), 403

    data = request.json
    if 'beginning_inventory' in data:
        inv.beginning_inventory = int(data['beginning_inventory'])
    if 'replenishment' in data:
        inv.replenishment = int(data['replenishment'])

    # Auto-calculate ending inventory using the formula:
    # Ending = Beginning + Replenishment − (Units Sold − Returns)
    units_sold, returns = _get_sales_for(inv.site_id, inv.product_id)
    net_sold            = units_sold - returns
    computed_ending     = (inv.beginning_inventory or 0) + (inv.replenishment or 0) - net_sold
    inv.ending_inventory = max(0, computed_ending)

    # Auto-set stockout flag based on computed ending
    inv.stockout_flag = 'Yes' if computed_ending <= 0 else 'No'

    # ── Auto-update StockLevel ──────────────────────────────────────────────
    _sync_stock_level(
        inv,
        current_user=current_user,
        movement_type='INVENTORY_UPDATE',
        notes=f'Stock synced after inventory update (net_sold={net_sold})',
    )
    # ───────────────────────────────────────────────────────────────────────

    db.session.commit()
    log_audit(current_user, 'UPDATE', 'Inventory', inv_id)
    return jsonify({'success': True, 'message': 'Inventory record updated', 'data': _format_item(inv)})


@bp.route('/<int:inv_id>', methods=['DELETE'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def delete_inventory(current_user, inv_id):
    inv = Inventory.query.get(inv_id)
    if not inv:
        return jsonify({'success': False, 'message': 'Not found'}), 404

    allowed = _allowed_site_ids(current_user)
    if allowed is not None and inv.site_id not in allowed:
        return jsonify({'success': False, 'message': 'Forbidden'}), 403

    db.session.delete(inv)
    db.session.commit()
    log_audit(current_user, 'DELETE', 'Inventory', inv_id)
    return jsonify({'success': True, 'message': 'Inventory record deleted'})


@bp.route('/sites', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_sites(current_user):
    allowed = _allowed_site_ids(current_user)
    if allowed is None:
        sites = Site.query.order_by(Site.site_name).all()
    else:
        sites = Site.query.filter(Site.site_id.in_(allowed)).order_by(Site.site_name).all()
    return jsonify({'success': True, 'data': [
        {'site_id': s.site_id, 'site_name': s.site_name} for s in sites
    ]})


@bp.route('/products', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_products(current_user):
    search = request.args.get('q', '').strip()
    query  = Product.query.filter_by(status='Active')
    if search:
        query = query.filter(or_(
            Product.product_name.ilike(f'%{search}%'),
            Product.product_id.ilike(f'%{search}%'),
        ))
    products = query.order_by(Product.product_name).limit(100).all()
    return jsonify({'success': True, 'data': [
        {'product_id': p.product_id, 'product_name': p.product_name, 'category': p.category}
        for p in products
    ]})


@bp.route('/summary', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_summary(current_user):
    query   = Inventory.query
    allowed = _allowed_site_ids(current_user)
    if allowed is not None:
        query = query.filter(Inventory.site_id.in_(allowed))

    all_items    = query.all()
    total        = len(all_items)
    stockouts    = sum(1 for i in all_items if i.stockout_flag == 'Yes')
    avg_ending   = round(sum(i.ending_inventory or 0 for i in all_items) / total, 1) if total else 0
    total_replen = sum(i.replenishment or 0 for i in all_items)

    return jsonify({'success': True, 'data': {
        'total_records':        total,
        'stockout_count':       stockouts,
        'avg_ending_inventory': avg_ending,
        'total_replenishment':  total_replen,
    }})


@bp.route('/repair-sale-records', methods=['POST'])
@token_required
@role_required('Admin')
@handle_exceptions
def repair_sale_records(current_user):
    """
    One-time repair: reset Sale.units_sold and Sale.revenue to match actual
    SalesOrderItem shipped quantities and line totals, preventing double-counts
    and NULL revenue introduced by the earlier ship/deliver hooks.
    Admin-only.
    """
    correct = db.session.query(
        SalesOrder.warehouse_id.label('site_id'),
        SalesOrder.order_date.label('order_date'),
        SalesOrder.customer_id.label('customer_id'),
        SalesOrderItem.product_id,
        func.sum(SalesOrderItem.shipped_quantity).label('shipped'),
        func.sum(
            SalesOrderItem.shipped_quantity * SalesOrderItem.unit_price
        ).label('revenue')
    ).join(
        SalesOrderItem, SalesOrderItem.so_id == SalesOrder.id
    ).filter(
        SalesOrder.status.in_(['Shipped', 'Delivered']),
        SalesOrderItem.shipped_quantity > 0
    ).group_by(
        SalesOrder.warehouse_id, SalesOrder.order_date,
        SalesOrder.customer_id, SalesOrderItem.product_id
    ).all()

    fixed = 0
    for row in correct:
        sale_rec = Sale.query.filter_by(
            site_id=row.site_id, product_id=row.product_id
        ).first()
        correct_qty = int(row.shipped or 0)
        correct_rev = float(row.revenue or 0)
        if sale_rec:
            if sale_rec.units_sold != correct_qty or float(sale_rec.revenue or 0) != correct_rev:
                sale_rec.units_sold = correct_qty
                sale_rec.revenue    = correct_rev
                fixed += 1
        else:
            db.session.add(Sale(
                site_id=row.site_id,
                product_id=row.product_id,
                units_sold=correct_qty,
                revenue=correct_rev,
                returns=0,
                date=row.order_date,
                customer_id=row.customer_id,
            ))
            fixed += 1

    db.session.commit()
    return jsonify({'success': True, 'message': f'Repaired {fixed} Sale record(s).',
                    'fixed_count': fixed})
