from flask import Blueprint, jsonify, request
from app.models import (Sale, Product, StockLevel, Site, Customer, Manager,
                        User, Role, Logistics, PurchaseOrder, SalesOrder,
                        SalesOrderItem, PurchaseOrderItem, StockMovement,
                        SeasonalPlan, Inventory, db)
from app.auth import token_required
from app.dependencies import handle_exceptions
from sqlalchemy import func, case, and_
from datetime import datetime, timedelta,timezone

bp = Blueprint('analytics', __name__, url_prefix='/api/analytics')


def _manager_site_ids(current_user):
    if current_user.role.role_name in ('Manager', 'Analyst') and current_user.state_id and current_user.state_id != 'ALL':
        try:
            sites = Site.query.filter_by(state_id=int(current_user.state_id)).all()
            return [s.site_id for s in sites]
        except (ValueError, TypeError):
            return []
    return None


def _analyst_site_ids(current_user, state_id_override=None):
    """Return site_ids for the analyst's/manager's assigned state. None means no restriction (ALL).
    If state_id_override is provided (from query param), filter by that state instead.
    Analysts with state_id='ALL' can filter any state via override."""
    role_name = current_user.role.role_name if current_user.role else ''

    # If a specific state filter is requested (and user has access)
    if state_id_override and state_id_override != 'ALL':
        # Only allow if user is Admin, or Analyst with ALL access, or user assigned to that exact state
        if role_name == 'Admin' or current_user.state_id == 'ALL' or str(current_user.state_id) == str(state_id_override):
            try:
                sites = Site.query.filter_by(state_id=int(state_id_override)).all()
                return [s.site_id for s in sites]
            except (ValueError, TypeError):
                return []

    # Default: use the analyst's or manager's own assigned state
    if role_name in ('Analyst', 'Manager') and current_user.state_id and current_user.state_id != 'ALL':
        try:
            sites = Site.query.filter_by(state_id=int(current_user.state_id)).all()
            return [s.site_id for s in sites]
        except (ValueError, TypeError):
            return []
    return None


@bp.route('/sales-trends', methods=['GET'])
@token_required
@handle_exceptions
def sales_trends(current_user):
    six_months_ago = datetime.now() - timedelta(days=180)
    query = db.session.query(
        func.to_char(Sale.date, 'Mon YYYY').label('month'),
        func.sum(Sale.revenue).label('revenue')
    ).filter(Sale.date >= six_months_ago)
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(Sale.site_id.in_(site_ids))
    results = query.group_by(func.to_char(Sale.date, 'Mon YYYY'), func.date_trunc('month', Sale.date))\
        .order_by(func.date_trunc('month', Sale.date).asc()).all()
    labels = [r.month for r in results]
    data = [float(r.revenue or 0) for r in results]
    return jsonify({'success': True, 'data': {
        'labels': labels if labels else ['No Data'],
        'revenue': data if data else [0]
    }})


@bp.route('/top-products', methods=['GET'])
@token_required
@handle_exceptions
def top_products(current_user):
    limit = request.args.get('limit', 10, type=int)
    query = db.session.query(
        Product.product_name,
        func.sum(Sale.revenue).label('total_revenue')
    ).join(Sale, Product.product_id == Sale.product_id)
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(Sale.site_id.in_(site_ids))
    results = query.group_by(Product.product_name)\
        .order_by(func.sum(Sale.revenue).desc()).limit(limit).all()
    return jsonify({'success': True, 'data': [{
        'product_name': r.product_name, 'name': r.product_name,
        'revenue': float(r.total_revenue or 0)
    } for r in results]})


@bp.route('/about-stats', methods=['GET'])
def about_stats():
    try:
        from app.models import Supplier, Logistics as _Logistics
        total_warehouses = Site.query.count()
        total_products   = Product.query.filter_by(status='Active').count()
        total_customers  = Customer.query.count()
        total_managers   = User.query.join(Role).filter(Role.role_name == 'Manager', User.is_active == True).count()
        total_stock      = db.session.query(func.coalesce(func.sum(StockLevel.current_quantity), 0)).scalar()
        total_suppliers  = Supplier.query.count()
        total_shipments  = _Logistics.query.count()
        return jsonify({'success': True, 'data': {
            'warehouses': total_warehouses, 'products': total_products,
            'customers': total_customers, 'managers': total_managers,
            'stock_units': int(total_stock), 'suppliers': total_suppliers,
            'shipments': total_shipments,
        }})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)}), 500


@bp.route('/category-performance', methods=['GET'])
@token_required
@handle_exceptions
def category_performance(current_user):
    query = db.session.query(
        Product.category,
        func.sum(Sale.revenue).label('revenue')
    ).join(Sale, Product.product_id == Sale.product_id)
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(Sale.site_id.in_(site_ids))
    results = query.group_by(Product.category).order_by(func.sum(Sale.revenue).desc()).all()
    return jsonify({'success': True, 'data': [{
        'category': r.category or 'General', 'revenue': float(r.revenue or 0)
    } for r in results]})


# ─── ANALYST ENDPOINTS ───────────────────────────────────────


def _latest_sale_date():
    """Return the most recent sale date in the DB.
    IMPORTANT: Never fall back to the system clock — the sales dataset is
    historical (ends ~May 2025).  Using datetime.utcnow() would push
    month_start into 2026+ and make every revenue filter return zero.
    If there are genuinely no sales, return a fixed anchor so callers get
    sensible empty results instead of wrong ones.
    """
    d = db.session.query(func.max(Sale.date)).scalar()
    if d:
        return d
    # Absolute fallback: first day of the current calendar month so at
    # least the time-window makes sense even with an empty database.
    today = datetime.now(timezone.utc).date()
    return today.replace(day=1)

@bp.route('/analyst/kpi-summary', methods=['GET'])
@token_required
@handle_exceptions
def analyst_kpi_summary(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)

    # Use the latest date in the sales data as reference (data may be historical).
    # NEVER fall back to datetime.utcnow() — the sales data ends ~May 2025,
    # so a 2026 system clock would make month_start 2026-05-01 and all
    # revenue queries would return zero.
    base_q = db.session.query(func.max(Sale.date))
    if site_ids is not None:
        base_q = base_q.filter(Sale.site_id.in_(site_ids))
    latest_date = base_q.scalar()
    if latest_date is None:
        # Fallback: use the global latest sale date (ignoring site scope)
        latest_date = _latest_sale_date()
    today = latest_date
    month_start = today.replace(day=1)
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    now = datetime.now(timezone.utc)

    def sale_q():
        q = db.session.query(func.coalesce(func.sum(Sale.revenue), 0))
        if site_ids is not None:
            q = q.filter(Sale.site_id.in_(site_ids))
        return q

    rev_this = sale_q().filter(Sale.date >= month_start).scalar()
    rev_prev = sale_q().filter(Sale.date.between(prev_month_start, prev_month_end)).scalar()
    rev_change = ((float(rev_this) - float(rev_prev)) / float(rev_prev) * 100) if rev_prev else 0

    units_q = db.session.query(func.coalesce(func.sum(Sale.units_sold), 0)).filter(Sale.date >= month_start)
    if site_ids is not None:
        units_q = units_q.filter(Sale.site_id.in_(site_ids))
    units_this = units_q.scalar()

    # Stock: scoped to sites in the analyst's state
    stock_q = db.session.query(
        StockLevel.product_id,
        func.sum(StockLevel.current_quantity).label('total_qty')
    )
    if site_ids is not None:
        stock_q = stock_q.filter(StockLevel.site_id.in_(site_ids))
    agg = stock_q.group_by(StockLevel.product_id).subquery()
    low_stock_count = db.session.query(func.count())\
        .select_from(Product)\
        .join(agg, Product.product_id == agg.c.product_id)\
        .filter(agg.c.total_qty <= Product.reorder_point).scalar()

    inv_q = db.session.query(
        func.coalesce(func.sum(StockLevel.current_quantity * Product.unit_cost), 0)
    ).join(Product, StockLevel.product_id == Product.product_id)
    if site_ids is not None:
        inv_q = inv_q.filter(StockLevel.site_id.in_(site_ids))
    inv_value = inv_q.scalar()

    cust_q = db.session.query(func.count(func.distinct(Sale.customer_id)))\
        .filter(Sale.date >= month_start, Sale.customer_id.isnot(None))
    if site_ids is not None:
        cust_q = cust_q.filter(Sale.site_id.in_(site_ids))
    active_customers = cust_q.scalar()

    ship_q = Logistics.query.filter(Logistics.delivery_status.in_(['Pending', 'In Transit', 'Shipped']))
    if site_ids is not None:
        ship_q = ship_q.filter(Logistics.site_id.in_(site_ids))
    pending_shipments = ship_q.count()

    today_q = db.session.query(func.coalesce(func.sum(Sale.revenue), 0)).filter(Sale.date == latest_date)
    if site_ids is not None:
        today_q = today_q.filter(Sale.site_id.in_(site_ids))
    today_sales = today_q.scalar()

    ret_q = db.session.query(func.coalesce(func.sum(Sale.returns), 0)).filter(Sale.date >= month_start)
    if site_ids is not None:
        ret_q = ret_q.filter(Sale.site_id.in_(site_ids))
    total_returns = ret_q.scalar()

    # ── Total Purchase Orders (all time, scoped to state) ─────────────────
    po_q = db.session.query(func.count(PurchaseOrder.id))
    if site_ids is not None:
        po_q = po_q.filter(PurchaseOrder.warehouse_id.in_(site_ids))
    total_purchase_orders = po_q.scalar() or 0

    # ── Total Sales Orders (all time, scoped to state) ────────────────────
    so_q = db.session.query(func.count(SalesOrder.id))
    if site_ids is not None:
        so_q = so_q.filter(SalesOrder.warehouse_id.in_(site_ids))
    total_sales_orders = so_q.scalar() or 0

    # ── Out of Stock (matches Inventory Master stockout_flag) ─────────────
    oos_q = db.session.query(func.count(func.distinct(Inventory.product_id)))        .filter(Inventory.stockout_flag == 'Yes')
    if site_ids is not None:
        oos_q = oos_q.filter(Inventory.site_id.in_(site_ids))
    out_of_stock_count = oos_q.scalar() or 0

    # ── Total Products (from products catalog, Active only) ───────────────
    total_products = Product.query.filter_by(status='Active').count()

    return jsonify({'success': True, 'data': {
        'revenue_this_month':    float(rev_this),
        'revenue_prev_month':    float(rev_prev),
        'revenue_change_pct':    round(rev_change, 1),
        'units_sold_this_month': int(units_this),
        'low_stock_count':       int(low_stock_count),
        'inventory_value':       float(inv_value),
        'active_customers':      int(active_customers),
        'pending_shipments':     int(pending_shipments),
        'today_sales':           float(today_sales),
        'total_returns':         int(total_returns),
        'total_purchase_orders': int(total_purchase_orders),
        'total_sales_orders':    int(total_sales_orders),
        'out_of_stock_count':    int(out_of_stock_count),
        'total_products':        int(total_products),
        'last_updated':          f"Data up to {latest_date.strftime('%Y-%m-%d')} · Fetched {now.strftime('%H:%M UTC')}",
    }})


@bp.route('/analyst/recent-sales', methods=['GET'])
@token_required
@handle_exceptions
def analyst_recent_sales(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)
    q = db.session.query(Sale, Product, Site)\
        .join(Product, Sale.product_id == Product.product_id)\
        .join(Site, Sale.site_id == Site.site_id)
    if site_ids is not None:
        q = q.filter(Sale.site_id.in_(site_ids))
    results = q.order_by(Sale.date.desc(), Sale.id.desc()).limit(20).all()
    return jsonify({'success': True, 'data': [{
        'id': s.id, 'date': s.date.strftime('%Y-%m-%d') if s.date else '',
        'product_name': p.product_name, 'category': p.category or 'General',
        'warehouse': w.site_name, 'units_sold': s.units_sold,
        'revenue': float(s.revenue or 0), 'discounts': float(s.discounts or 0),
        'returns': s.returns or 0,
    } for s, p, w in results]})


@bp.route('/analyst/sales-trend-12m', methods=['GET'])
@token_required
@handle_exceptions
def analyst_sales_trend_12m(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)

    ref = _latest_sale_date()
    twelve_months_ago = ref - timedelta(days=365)

    month_label = func.to_char(
        Sale.date,
        'Mon YYYY'
    ).label('month')

    month_dt = func.date_trunc(
        'month',
        Sale.date
    ).label('month_dt')

    q = db.session.query(
        month_label,
        month_dt,
        func.sum(Sale.revenue).label('revenue'),
        func.sum(Sale.units_sold).label('units'),
        func.sum(Sale.returns).label('returns')
    ).filter(
        Sale.date >= twelve_months_ago
    )

    if site_ids is not None:
        q = q.filter(Sale.site_id.in_(site_ids))

    results = q.group_by(
        month_label,
        month_dt
    ).order_by(
        month_dt.asc()
    ).all()

    return jsonify({
        'success': True,
        'data': {
            'labels':  [r.month for r in results],
            'revenue': [float(r.revenue or 0) for r in results],
            'units':   [int(r.units or 0) for r in results],
            'returns': [int(r.returns or 0) for r in results],
        }
    })

@bp.route('/analyst/stock-health', methods=['GET'])
@token_required
@handle_exceptions
def analyst_stock_health(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)

    # ── Aggregate current stock from StockLevel (INNER JOIN only — products
    #    that have no StockLevel record are not yet stocked and should not be
    #    counted as "Out of Stock").
    stock_q = db.session.query(
        StockLevel.product_id,
        func.sum(StockLevel.current_quantity).label('total_qty')
    )
    if site_ids is not None:
        stock_q = stock_q.filter(StockLevel.site_id.in_(site_ids))
    agg = stock_q.group_by(StockLevel.product_id).subquery()

    # INNER JOIN: only products that actually have StockLevel records are included.
    # This keeps the "Out of Stock" count consistent with the Inventory Master,
    # which uses the stockout_flag on the Inventory table.
    results = db.session.query(Product, agg.c.total_qty)\
        .join(agg, Product.product_id == agg.c.product_id).all()

    # ── Also pull stockout flags from the Inventory table so the dashboard
    #    "Out of Stock" count matches the Inventory Master exactly.
    from app.models import Inventory
    inv_q = db.session.query(Inventory.product_id).filter(Inventory.stockout_flag == 'Yes')
    if site_ids is not None:
        inv_q = inv_q.filter(Inventory.site_id.in_(site_ids))
    stockout_products = {row.product_id for row in inv_q.all()}

    healthy = critical = low = out = 0
    detail = []
    for p, qty in results:
        qty = int(qty or 0)
        # Use stockout_flag from Inventory table as the authoritative source
        if p.product_id in stockout_products or qty == 0:
            out += 1; status = 'Out of Stock'
        elif qty <= p.reorder_point:
            critical += 1; status = 'Critical'
        elif qty <= p.reorder_point * 1.5:
            low += 1; status = 'Low'
        else:
            healthy += 1; status = 'Healthy'
        detail.append({
            'product_id': p.product_id, 'product_name': p.product_name,
            'category': p.category or 'General', 'quantity': qty,
            'reorder_point': p.reorder_point, 'status': status,
        })
    detail.sort(key=lambda x: x['quantity'])
    return jsonify({'success': True, 'data': {
        'summary': {'healthy': healthy, 'low': low, 'critical': critical, 'out_of_stock': out},
        'items': detail[:50],
    }})


@bp.route('/analyst/logistics-performance', methods=['GET'])
@token_required
@handle_exceptions
def analyst_logistics_performance(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)

    status_q = db.session.query(Logistics.delivery_status, func.count(Logistics.id).label('count'))
    if site_ids is not None:
        status_q = status_q.filter(Logistics.site_id.in_(site_ids))
    status_results = status_q.group_by(Logistics.delivery_status).all()

    transport_q = db.session.query(Logistics.transportation_type, func.count(Logistics.id).label('count'))
    if site_ids is not None:
        transport_q = transport_q.filter(Logistics.site_id.in_(site_ids))
    transport_results = transport_q.group_by(Logistics.transportation_type).all()

    recent_q = db.session.query(Logistics, Product, Site)\
        .join(Product, Logistics.product_id == Product.product_id)\
        .join(Site, Logistics.site_id == Site.site_id)
    if site_ids is not None:
        recent_q = recent_q.filter(Logistics.site_id.in_(site_ids))
    recent = recent_q.order_by(Logistics.shipment_date.desc()).limit(15).all()

    return jsonify({'success': True, 'data': {
        'status_breakdown': [{'status': r.delivery_status, 'count': r.count} for r in status_results],
        'transport_types':  [{'type': r.transportation_type, 'count': r.count} for r in transport_results],
        'recent_shipments': [{
            'shipment_id': l.shipment_id,
            'date': l.shipment_date.strftime('%Y-%m-%d') if l.shipment_date else '',
            'product': p.product_name, 'warehouse': w.site_name,
            'quantity': l.quantity, 'status': l.delivery_status,
            'transport': l.transportation_type,
        } for l, p, w in recent],
    }})


@bp.route('/analyst/revenue-by-warehouse', methods=['GET'])
@token_required
@handle_exceptions
def analyst_revenue_by_warehouse(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)
    thirty_days_ago = _latest_sale_date() - timedelta(days=30)
    q = db.session.query(
        Site.site_name,
        func.sum(Sale.revenue).label('revenue'),
        func.sum(Sale.units_sold).label('units')
    ).join(Sale, Site.site_id == Sale.site_id).filter(Sale.date >= thirty_days_ago)
    if site_ids is not None:
        q = q.filter(Sale.site_id.in_(site_ids))
    results = q.group_by(Site.site_name).order_by(func.sum(Sale.revenue).desc()).all()
    return jsonify({'success': True, 'data': [{
        'warehouse': r.site_name, 'revenue': float(r.revenue or 0), 'units': int(r.units or 0),
    } for r in results]})



@bp.route('/analyst/revenue-by-category', methods=['GET'])
@token_required
@handle_exceptions
def analyst_revenue_by_category(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)
    q = db.session.query(
        Product.category,
        func.coalesce(func.sum(Sale.revenue), 0).label('revenue')
    ).join(Sale, Product.product_id == Sale.product_id)
    if site_ids is not None:
        q = q.filter(Sale.site_id.in_(site_ids))
    results = q.group_by(Product.category).order_by(func.sum(Sale.revenue).desc()).all()
    return jsonify({'success': True, 'data': [
        {'category': r.category or 'General', 'revenue': float(r.revenue)}
        for r in results
    ]})

@bp.route('/analyst/stockout-by-site', methods=['GET'])
@token_required
@handle_exceptions
def analyst_stockout_by_site(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)
    q = db.session.query(
        Site.site_name,
        func.count(Inventory.id).label('stockouts')
    ).join(Inventory, Site.site_id == Inventory.site_id)     .filter(Inventory.stockout_flag == 'Yes')
    if site_ids is not None:
        q = q.filter(Inventory.site_id.in_(site_ids))
    results = q.group_by(Site.site_name).order_by(func.count(Inventory.id).desc()).all()
    return jsonify({'success': True, 'data': [
        {'site': r.site_name, 'stockouts': r.stockouts} for r in results
    ]})


@bp.route('/analyst/purchase-frequency', methods=['GET'])
@token_required
@handle_exceptions
def analyst_purchase_frequency(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)
    # Count POs per product to build a frequency distribution
    q = db.session.query(
        PurchaseOrderItem.product_id,
        func.count(PurchaseOrderItem.id).label('order_count')
    ).join(PurchaseOrder, PurchaseOrderItem.po_id == PurchaseOrder.id)
    if site_ids is not None:
        q = q.filter(PurchaseOrder.warehouse_id.in_(site_ids))
    results = q.group_by(PurchaseOrderItem.product_id).all()
    counts = [r.order_count for r in results]
    if not counts:
        return jsonify({'success': True, 'data': {'labels': [], 'values': []}})
    # Build histogram bins
    max_c = max(counts)
    bin_size = max(1, max_c // 8)
    bins = {}
    for c in counts:
        b = (c // bin_size) * bin_size
        bins[b] = bins.get(b, 0) + 1
    sorted_bins = sorted(bins.items())
    labels = [f'{b}–{b+bin_size-1}' for b, _ in sorted_bins]
    values = [v for _, v in sorted_bins]
    return jsonify({'success': True, 'data': {'labels': labels, 'values': values}})


@bp.route('/analyst/customer-insights', methods=['GET'])
@token_required
@handle_exceptions
def analyst_customer_insights(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)

    top_q = db.session.query(
        Customer.customer_id, Customer.name, Customer.income_bracket,
        func.sum(Sale.revenue).label('total_revenue'),
        func.count(Sale.id).label('total_orders'),
    ).join(Sale, Customer.customer_id == Sale.customer_id)
    if site_ids is not None:
        top_q = top_q.filter(Sale.site_id.in_(site_ids))
    top_customers = top_q.group_by(Customer.customer_id, Customer.name, Customer.income_bracket)\
        .order_by(func.sum(Sale.revenue).desc()).limit(10).all()

    # income/gender breakdown scoped to customers who bought in this state
    if site_ids is not None:
        scoped_customer_ids = db.session.query(func.distinct(Sale.customer_id))\
            .filter(Sale.site_id.in_(site_ids), Sale.customer_id.isnot(None)).subquery()
        income_q = db.session.query(Customer.income_bracket, func.count(Customer.customer_id).label('count'))\
            .filter(Customer.customer_id.in_(scoped_customer_ids))
        gender_q = db.session.query(Customer.gender, func.count(Customer.customer_id).label('count'))\
            .filter(Customer.customer_id.in_(scoped_customer_ids))
    else:
        income_q = db.session.query(Customer.income_bracket, func.count(Customer.customer_id).label('count'))
        gender_q = db.session.query(Customer.gender, func.count(Customer.customer_id).label('count'))

    income_breakdown = income_q.group_by(Customer.income_bracket).all()
    gender_breakdown = gender_q.group_by(Customer.gender).all()

    return jsonify({'success': True, 'data': {
        'top_customers': [{
            'name': c.name or c.customer_id,
            'type': c.income_bracket or 'Standard',
            'total_revenue': float(c.total_revenue or 0),
            'total_orders': int(c.total_orders or 0),
        } for c in top_customers],
        'type_breakdown': [{'type': t.income_bracket or 'Unknown', 'count': t.count} for t in income_breakdown],
        'gender_breakdown': [{'gender': g.gender or 'Unknown', 'count': g.count} for g in gender_breakdown],
    }})


@bp.route('/analyst/purchase-order-analytics', methods=['GET'])
@token_required
@handle_exceptions
def analyst_po_analytics(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)

    status_q = db.session.query(
        PurchaseOrder.status,
        func.count(PurchaseOrder.id).label('count'),
        func.coalesce(func.sum(PurchaseOrder.total_amount), 0).label('total_value')
    )
    if site_ids is not None:
        status_q = status_q.filter(PurchaseOrder.warehouse_id.in_(site_ids))
    status_results = status_q.group_by(PurchaseOrder.status).all()

    six_months_ago = datetime.now() - timedelta(days=180)
    monthly_q = db.session.query(
        func.to_char(PurchaseOrder.order_date, 'Mon YYYY').label('month'),
        func.date_trunc('month', PurchaseOrder.order_date).label('month_dt'),
        func.count(PurchaseOrder.id).label('count'),
        func.coalesce(func.sum(PurchaseOrder.total_amount), 0).label('spend')
    ).filter(PurchaseOrder.order_date >= six_months_ago)
    if site_ids is not None:
        monthly_q = monthly_q.filter(PurchaseOrder.warehouse_id.in_(site_ids))
    monthly_po = monthly_q\
        .group_by(func.to_char(PurchaseOrder.order_date, 'Mon YYYY'), func.date_trunc('month', PurchaseOrder.order_date))\
        .order_by(func.date_trunc('month', PurchaseOrder.order_date).asc()).all()

    return jsonify({'success': True, 'data': {
        'status_breakdown': [{'status': r.status, 'count': r.count, 'value': float(r.total_value)} for r in status_results],
        'monthly_trend': {
            'labels': [r.month for r in monthly_po],
            'spend':  [float(r.spend) for r in monthly_po],
            'count':  [r.count for r in monthly_po],
        }
    }})


@bp.route('/analyst/forecast-vs-actual', methods=['GET'])
@token_required
@handle_exceptions
def analyst_forecast_vs_actual(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)
    q = db.session.query(
        SeasonalPlan.month,
        func.sum(SeasonalPlan.forecasted_sales).label('forecasted'),
        func.sum(SeasonalPlan.actual_sales).label('actual')
    )
    if site_ids is not None:
        q = q.filter(SeasonalPlan.site_id.in_(site_ids))
    results = q.group_by(SeasonalPlan.month).order_by(SeasonalPlan.month).all()

    return jsonify({'success': True, 'data': {
        'labels':     [r.month for r in results],
        'forecasted': [float(r.forecasted or 0) for r in results],
        'actual':     [float(r.actual or 0) for r in results],
    }})


@bp.route('/analyst/state-comparison', methods=['GET'])
@token_required
@handle_exceptions
def analyst_state_comparison(current_user):
    """Return revenue & units summary per state for the Analyst (ALL-states view)."""

    role_name = current_user.role.role_name if current_user.role else ''

    allowed = (
        role_name == 'Admin' or
        (role_name in ('Analyst', 'Manager') and current_user.state_id == 'ALL')
    )

    if not allowed:
        return jsonify({
            'success': False,
            'message': 'Access denied'
        }), 403

    from app.models import State

    ref = _latest_sale_date()

    thirty_days_ago = ref - timedelta(days=30)
    twelve_months_ago = ref - timedelta(days=365)

    # Revenue per state (last 30 days)
    results = db.session.query(
        State.state_name,
        State.state_id,
        func.coalesce(func.sum(Sale.revenue), 0).label('revenue_30d'),
        func.coalesce(func.sum(Sale.units_sold), 0).label('units_30d'),
        func.coalesce(func.sum(Sale.returns), 0).label('returns_30d'),
    ).join(
        Site,
        Site.state_id == State.state_id
    ).outerjoin(
        Sale,
        and_(
            Sale.site_id == Site.site_id,
            Sale.date >= thirty_days_ago
        )
    ).group_by(
        State.state_id,
        State.state_name
    ).order_by(
        func.coalesce(func.sum(Sale.revenue), 0).desc()
    ).all()

    # 12-month trend per state
    month_label = func.to_char(
        Sale.date,
        'Mon YYYY'
    ).label('month')

    month_dt = func.date_trunc(
        'month',
        Sale.date
    ).label('month_dt')

    trend_results = db.session.query(
        State.state_name,
        month_label,
        month_dt,
        func.coalesce(func.sum(Sale.revenue), 0).label('revenue'),
    ).join(
        Site,
        Site.state_id == State.state_id
    ).join(
        Sale,
        and_(
            Sale.site_id == Site.site_id,
            Sale.date >= twelve_months_ago
        )
    ).group_by(
        State.state_name,
        month_label,
        month_dt
    ).order_by(
        month_dt.asc()
    ).all()

    # Stock health per state
    stock_results = db.session.query(
        State.state_name,
        func.coalesce(
            func.sum(
                case(
                    (StockLevel.current_quantity <= Product.reorder_point, 1),
                    else_=0
                )
            ),
            0
        ).label('low_stock'),

        func.coalesce(
            func.sum(StockLevel.current_quantity),
            0
        ).label('total_stock'),

    ).join(
        Site,
        Site.state_id == State.state_id
    ).outerjoin(
        StockLevel,
        StockLevel.site_id == Site.site_id
    ).outerjoin(
        Product,
        Product.product_id == StockLevel.product_id
    ).group_by(
        State.state_id,
        State.state_name
    ).all()

    # Build trend by state
    trend_by_state = {}

    for r in trend_results:
        if r.state_name not in trend_by_state:
            trend_by_state[r.state_name] = {}

        trend_by_state[r.state_name][r.month] = float(r.revenue)

    stock_by_state = {
        r.state_name: {
            'low_stock': int(r.low_stock),
            'total_stock': int(r.total_stock)
        }
        for r in stock_results
    }

    return jsonify({
        'success': True,
        'data': {
            'states': [{
                'state_name': r.state_name,
                'state_id': r.state_id,
                'revenue_30d': float(r.revenue_30d),
                'units_30d': int(r.units_30d),
                'returns_30d': int(r.returns_30d),
                'low_stock': stock_by_state.get(
                    r.state_name,
                    {}
                ).get('low_stock', 0),
                'total_stock': stock_by_state.get(
                    r.state_name,
                    {}
                ).get('total_stock', 0),
            } for r in results],

            'trend_by_state': trend_by_state,
        }
    })

# ═══════════════════════════════════════════════════════════════════
#  NEW ANALYTICS ENDPOINTS — 12 missing visualizations
# ═══════════════════════════════════════════════════════════════════

# ── Sales: Daily Pattern ─────────────────────────────────────────
@bp.route('/analyst/daily-sales-pattern', methods=['GET'])
@token_required
@handle_exceptions
def analyst_daily_sales_pattern(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)
    q = db.session.query(
        func.extract('dow', Sale.date).label('dow'),
        func.avg(Sale.revenue).label('avg_revenue'),
        func.count(Sale.id).label('txn_count')
    )
    if site_ids is not None:
        q = q.filter(Sale.site_id.in_(site_ids))
    results = q.group_by(func.extract('dow', Sale.date)).order_by(func.extract('dow', Sale.date)).all()
    day_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    data = {int(r.dow): {'avg_revenue': float(r.avg_revenue or 0), 'txn_count': int(r.txn_count)} for r in results}
    return jsonify({'success': True, 'data': {
        'labels':      [day_names[i] for i in range(7)],
        'avg_revenue': [round(data.get(i, {}).get('avg_revenue', 0), 2) for i in range(7)],
        'txn_count':   [data.get(i, {}).get('txn_count', 0) for i in range(7)],
    }})


# ── Inventory: Turnover Rate by Category ─────────────────────────
@bp.route('/analyst/inventory-turnover', methods=['GET'])
@token_required
@handle_exceptions
def analyst_inventory_turnover(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)
    q = db.session.query(
        Product.category,
        func.sum(Inventory.beginning_inventory).label('total_begin'),
        func.sum(Inventory.replenishment).label('total_replenish')
    ).join(Product, Inventory.product_id == Product.product_id)
    if site_ids is not None:
        q = q.filter(Inventory.site_id.in_(site_ids))
    results = q.group_by(Product.category).all()
    cats, rates = [], []
    for r in results:
        if r.total_begin and r.total_begin > 0:
            cats.append(r.category)
            rates.append(round(float(r.total_replenish) / float(r.total_begin), 4))
    return jsonify({'success': True, 'data': {'labels': cats, 'rates': rates}})


# ── Inventory: Stock vs Sales Scatter ─────────────────────────────
@bp.route('/analyst/stock-vs-sales', methods=['GET'])
@token_required
@handle_exceptions
def analyst_stock_vs_sales(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)

    inv_q = db.session.query(
        Inventory.product_id,
        func.avg(Inventory.ending_inventory).label('avg_stock')
    )
    if site_ids is not None:
        inv_q = inv_q.filter(Inventory.site_id.in_(site_ids))
    inv_data = {r.product_id: float(r.avg_stock) for r in inv_q.group_by(Inventory.product_id).all()}

    sales_q = db.session.query(
        Sale.product_id,
        func.sum(Sale.revenue).label('total_revenue'),
        func.sum(Sale.units_sold).label('total_units')
    )
    if site_ids is not None:
        sales_q = sales_q.filter(Sale.site_id.in_(site_ids))
    sales_data = {r.product_id: {'revenue': float(r.total_revenue or 0), 'units': int(r.total_units or 0)}
                  for r in sales_q.group_by(Sale.product_id).all()}

    points = []
    for pid, stock in inv_data.items():
        if pid in sales_data:
            points.append({
                'product_id': pid,
                'stock': round(stock, 1),
                'revenue': round(sales_data[pid]['revenue'], 2),
                'units': sales_data[pid]['units'],
            })
    return jsonify({'success': True, 'data': {'points': points}})


# ── Customer: Age Group Distribution ─────────────────────────────
@bp.route('/analyst/customer-age-distribution', methods=['GET'])
@token_required
@handle_exceptions
def analyst_customer_age_distribution(current_user):
    customers = db.session.query(Customer.age).all()
    groups = {'18-25': 0, '26-35': 0, '36-45': 0, '46-55': 0, '55+': 0}
    for (age,) in customers:
        if age is None: continue
        if age <= 25:   groups['18-25'] += 1
        elif age <= 35: groups['26-35'] += 1
        elif age <= 45: groups['36-45'] += 1
        elif age <= 55: groups['46-55'] += 1
        else:           groups['55+']   += 1
    return jsonify({'success': True, 'data': {
        'labels': list(groups.keys()),
        'counts': list(groups.values()),
    }})


# ── Customer: Gender × Income Avg Spend ──────────────────────────
@bp.route('/analyst/gender-income-spend', methods=['GET'])
@token_required
@handle_exceptions
def analyst_gender_income_spend(current_user):
    results = db.session.query(
        Customer.gender,
        Customer.income_bracket,
        func.avg(Customer.average_spend).label('avg_spend'),
        func.count(Customer.customer_id).label('count')
    ).group_by(Customer.gender, Customer.income_bracket).all()

    income_order = ['1-5 LPA', '10 LPA', '10-20 LPA', '20+ LPA']
    genders = sorted(set(r.gender for r in results if r.gender))
    data = {}
    for r in results:
        g = r.gender or 'Unknown'
        i = r.income_bracket or 'Unknown'
        if g not in data: data[g] = {}
        data[g][i] = round(float(r.avg_spend or 0), 2)

    incomes = [i for i in income_order if any(i in data.get(g, {}) for g in genders)]
    return jsonify({'success': True, 'data': {
        'income_labels': incomes,
        'genders': genders,
        'series': {g: [data.get(g, {}).get(i, 0) for i in incomes] for g in genders},
    }})


# ── Customer: Age vs Avg Spend Scatter ───────────────────────────
@bp.route('/analyst/age-vs-spend', methods=['GET'])
@token_required
@handle_exceptions
def analyst_age_vs_spend(current_user):
    results = db.session.query(Customer.age, Customer.average_spend, Customer.gender).all()
    points = [{'x': int(r.age), 'y': float(r.average_spend or 0), 'gender': r.gender or 'Unknown'}
              for r in results if r.age is not None and r.average_spend is not None]
    return jsonify({'success': True, 'data': {'points': points}})


# ── Logistics: Avg Delivery Time by Site ─────────────────────────
@bp.route('/analyst/delivery-time-by-site', methods=['GET'])
@token_required
@handle_exceptions
def analyst_delivery_time_by_site(current_user):
    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)
    status_days = case(
        (Logistics.delivery_status == 'Delivered',  2),
        (Logistics.delivery_status == 'In Transit', 3),
        (Logistics.delivery_status == 'Delayed',    5),
        else_=None
    )
    q = db.session.query(
        Logistics.site_id,
        func.avg(status_days).label('avg_days'),
        func.count(Logistics.id).label('shipment_count')
    ).filter(Logistics.delivery_status.in_(['Delivered', 'In Transit', 'Delayed']))
    if site_ids is not None:
        q = q.filter(Logistics.site_id.in_(site_ids))
    results = q.group_by(Logistics.site_id).order_by(func.avg(status_days)).all()
    return jsonify({'success': True, 'data': {
        'labels':    [r.site_id for r in results],
        'avg_days':  [round(float(r.avg_days or 0), 2) for r in results],
        'ship_count':[int(r.shipment_count) for r in results],
    }})


# ── Logistics: Shipping Volume Over Time ─────────────────────────
@bp.route('/analyst/shipping-volume-trend', methods=['GET'])
@token_required
@handle_exceptions
def analyst_shipping_volume_trend(current_user):

    state_id_override = request.args.get('state_id')
    site_ids = _analyst_site_ids(current_user, state_id_override)

    month_label = func.to_char(
        Logistics.shipment_date,
        'Mon-YY'
    ).label('month')

    month_dt = func.date_trunc(
        'month',
        Logistics.shipment_date
    ).label('month_dt')

    q = db.session.query(
        month_label,
        month_dt,
        func.sum(Logistics.quantity).label('total_qty'),
        func.count(Logistics.id).label('shipment_count')
    )

    if site_ids is not None:
        q = q.filter(Logistics.site_id.in_(site_ids))

    results = q.group_by(
        month_label,
        month_dt
    ).order_by(
        month_dt.asc()
    ).all()

    return jsonify({
        'success': True,
        'data': {
            'labels': [r.month for r in results],
            'quantities': [int(r.total_qty or 0) for r in results],
            'ship_count': [int(r.shipment_count or 0) for r in results],
        }
    })