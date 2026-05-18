from flask import Blueprint, request, jsonify
from app.models import (SalesOrder, SalesOrderItem, SalesOrderReturn,
                        SalesOrderReturnItem, StockLevel, StockMovement,
                        Product, Site, Customer, db)
from app.auth import token_required, log_audit
from app.dependencies import handle_exceptions
from datetime import datetime

bp_so_returns = Blueprint('so_returns', __name__, url_prefix='/api/sales-orders')


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _manager_site_ids(current_user):
    if current_user.role.role_name in ('Manager', 'Analyst') and \
            current_user.state_id and current_user.state_id != 'ALL':
        try:
            sites = Site.query.filter_by(state_id=int(current_user.state_id)).all()
            return [s.site_id for s in sites]
        except (ValueError, TypeError):
            return []
    return None


def _format_return(ret):
    so   = SalesOrder.query.get(ret.so_id)
    cust = Customer.query.get(so.customer_id) if so and so.customer_id else None
    site = Site.query.get(ret.warehouse_id) if ret.warehouse_id else None
    items = []
    for ri in ret.items:
        prod = Product.query.get(ri.product_id)
        items.append({
            'product_id':   ri.product_id,
            'product_name': prod.product_name if prod else ri.product_id,
            'category':     prod.category if prod else '',
            'return_qty':   ri.return_qty,
            'condition':    ri.condition,
            'damage_by':    ri.damage_by or '',
            'unit_price':   float(ri.unit_price or 0),
            'line_total':   float(ri.line_total or 0),
            'reason':       ri.reason or '',
        })
    return {
        'id':             ret.id,
        'return_number':  ret.return_number,
        'so_id':          ret.so_id,
        'so_number':      so.so_number if so else '—',
        'customer_name':  (cust.name if cust and cust.name else None)
                          or (so.customer_id if so else '—') or 'Walk-in',
        'warehouse_id':   ret.warehouse_id,
        'warehouse_name': site.site_name if site else ret.warehouse_id,
        'status':         ret.status,
        'total_refund':   float(ret.total_refund or 0),
        'notes':          ret.notes or '',
        'created_at':     ret.created_at.strftime('%Y-%m-%d') if ret.created_at else '',
        'processed_at':   ret.processed_at.strftime('%Y-%m-%d %H:%M') if ret.processed_at else None,
        'items':          items,
    }


# ── LIST ALL RETURNS (global, paginated) ──────────────────────────────────────
@bp_so_returns.route('/returns', methods=['GET'])
@token_required
@handle_exceptions
def list_all_so_returns(current_user):
    page      = request.args.get('page', 1, type=int)
    status    = request.args.get('status', '')
    search    = request.args.get('search', '').strip()
    warehouse = request.args.get('warehouse', '').strip()  # site_name filter

    query    = SalesOrderReturn.query
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(SalesOrderReturn.warehouse_id.in_(site_ids))

    if status:
        query = query.filter(SalesOrderReturn.status == status)

    # Search: match return_number OR so_number (via join)
    if search:
        query = query.outerjoin(SalesOrder, SalesOrderReturn.so_id == SalesOrder.id)
        query = query.filter(
            db.or_(
                SalesOrderReturn.return_number.ilike(f'%{search}%'),
                SalesOrder.so_number.ilike(f'%{search}%'),
            )
        )

    # Warehouse filter: match by site_name
    if warehouse:
        site = Site.query.filter(Site.site_name.ilike(warehouse)).first()
        if site:
            query = query.filter(SalesOrderReturn.warehouse_id == site.site_id)
        else:
            # no match → return empty
            query = query.filter(db.false())

    pagination = query.order_by(SalesOrderReturn.created_at.desc()).paginate(
        page=page, per_page=15, error_out=False)

    # Build warehouse list for frontend dropdown (all available sites)
    all_site_ids = _manager_site_ids(current_user)
    if all_site_ids is not None:
        sites = Site.query.filter(Site.site_id.in_(all_site_ids)).order_by(Site.site_name).all()
    else:
        sites = Site.query.order_by(Site.site_name).all()
    warehouses = [{'id': s.site_id, 'name': s.site_name} for s in sites]

    return jsonify({'success': True, 'data': {
        'items':        [_format_return(r) for r in pagination.items],
        'total':        pagination.total,
        'pages':        pagination.pages,
        'current_page': page,
        'warehouses':   warehouses,
    }})


# ── RETURNS FOR ONE SO ────────────────────────────────────────────────────────
@bp_so_returns.route('/<int:so_id>/returns', methods=['GET'])
@token_required
@handle_exceptions
def list_so_returns(current_user, so_id):
    so = SalesOrder.query.get(so_id)
    if not so:
        return jsonify({'success': False, 'message': 'Sales Order not found'}), 404
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and so.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    returns = SalesOrderReturn.query.filter_by(so_id=so_id)\
        .order_by(SalesOrderReturn.created_at.desc()).all()
    return jsonify({'success': True, 'data': [_format_return(r) for r in returns]})


# ── SUMMARY KPIs ──────────────────────────────────────────────────────────────
@bp_so_returns.route('/returns/summary', methods=['GET'])
@token_required
@handle_exceptions
def so_returns_summary(current_user):
    query    = SalesOrderReturn.query
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(SalesOrderReturn.warehouse_id.in_(site_ids))
    all_returns = query.all()
    return jsonify({'success': True, 'data': {
        'total_returns': len(all_returns),
        'pending':       sum(1 for r in all_returns if r.status == 'Pending'),
        'approved':      sum(1 for r in all_returns if r.status == 'Approved'),
        'rejected':      sum(1 for r in all_returns if r.status == 'Rejected'),
        'total_refund':  round(sum(float(r.total_refund or 0)
                                   for r in all_returns if r.status == 'Approved'), 2),
    }})


# ── INITIATE RETURN ───────────────────────────────────────────────────────────
@bp_so_returns.route('/<int:so_id>/returns', methods=['POST'])
@token_required
@handle_exceptions
def create_so_return(current_user, so_id):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403

    so = SalesOrder.query.get(so_id)
    if not so:
        return jsonify({'success': False, 'message': 'Sales Order not found'}), 404
    if so.status not in ('Shipped', 'Delivered'):
        return jsonify({'success': False,
                        'message': f'Only Shipped or Delivered orders can be returned. '
                                   f'Current status: {so.status}'}), 400

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and so.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    data       = request.json or {}
    items_data = data.get('items', [])
    if not items_data:
        return jsonify({'success': False, 'message': 'At least one item is required'}), 400

    # shipped qty per product
    shipped_map = {item.product_id: (item.shipped_quantity or item.quantity or 0)
                   for item in so.items}

    # already approved returns for this SO
    already_returned = {}
    for prev in SalesOrderReturn.query.filter_by(so_id=so_id, status='Approved').all():
        for ri in prev.items:
            already_returned[ri.product_id] = already_returned.get(ri.product_id, 0) + ri.return_qty

    # validate each item
    for ri in items_data:
        pid     = ri.get('product_id')
        ret_qty = int(ri.get('return_qty', 0))
        if ret_qty <= 0:
            return jsonify({'success': False,
                            'message': f'Return quantity must be > 0 for {pid}'}), 400
        shipped    = shipped_map.get(pid, 0)
        done       = already_returned.get(pid, 0)
        returnable = shipped - done
        if ret_qty > returnable:
            prod = Product.query.get(pid)
            name = prod.product_name if prod else pid
            return jsonify({'success': False,
                            'message': f'Cannot return {ret_qty} of "{name}". '
                                       f'Shipped: {shipped}, Already returned: {done}, '
                                       f'Returnable: {returnable}'}), 400
        if ri.get('condition') not in ('Good', 'Damaged'):
            return jsonify({'success': False,
                            'message': f'Condition must be Good or Damaged for {pid}'}), 400
        if ri.get('condition') == 'Damaged' and \
                ri.get('damage_by') not in ('our_damage', 'customer_damage'):
            return jsonify({'success': False,
                            'message': f'For damaged items, damage_by must be "our_damage" or "customer_damage" for {pid}'}), 400

    ret_num      = f"SORET-{so.so_number}-{datetime.now().strftime('%H%M%S')}"
    total_refund = 0.0

    for ri in items_data:
        pid        = ri.get('product_id')
        qty        = int(ri.get('return_qty', 0))
        cond       = ri.get('condition', 'Good')
        damage_by  = ri.get('damage_by', '')
        so_item    = next((i for i in so.items if i.product_id == pid), None)
        unit_price = float(so_item.unit_price or 0) if so_item else 0.0
        # Refund for Good items AND our-damage items (we take responsibility)
        if cond == 'Good' or (cond == 'Damaged' and damage_by == 'our_damage'):
            total_refund += qty * unit_price

    new_ret = SalesOrderReturn(
        return_number = ret_num,
        so_id         = so_id,
        warehouse_id  = so.warehouse_id,
        status        = 'Pending',
        total_refund  = round(total_refund, 2),
        notes         = data.get('notes', ''),
        created_by    = current_user.id,
    )
    db.session.add(new_ret)
    db.session.flush()

    for ri in items_data:
        pid        = ri.get('product_id')
        qty        = int(ri.get('return_qty', 0))
        cond       = ri.get('condition', 'Good')
        damage_by  = ri.get('damage_by', '') if cond == 'Damaged' else None
        so_item    = next((i for i in so.items if i.product_id == pid), None)
        unit_price = float(so_item.unit_price or 0) if so_item else 0.0
        # Refund for Good + our_damage; no refund for customer_damage
        has_refund = (cond == 'Good') or (cond == 'Damaged' and damage_by == 'our_damage')
        line_total = round(qty * unit_price, 2) if has_refund else 0.0

        db.session.add(SalesOrderReturnItem(
            return_id  = new_ret.id,
            product_id = pid,
            return_qty = qty,
            condition  = cond,
            damage_by  = damage_by,
            unit_price = unit_price,
            line_total = line_total,
            reason     = ri.get('reason', ''),
        ))

    db.session.commit()
    log_audit(current_user, 'CREATE_RETURN', 'SalesOrderReturn', new_ret.id,
              {'return_number': ret_num, 'so_number': so.so_number})

    return jsonify({
        'success':       True,
        'message':       f'Return {ret_num} initiated. Pending inspection.',
        'return_id':     new_ret.id,
        'return_number': ret_num,
        'total_refund':  float(new_ret.total_refund),
    })


# ── PROCESS RETURN (Approve / Reject) ────────────────────────────────────────
@bp_so_returns.route('/returns/<int:return_id>/process', methods=['POST'])
@token_required
@handle_exceptions
def process_so_return(current_user, return_id):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403

    ret = SalesOrderReturn.query.get(return_id)
    if not ret:
        return jsonify({'success': False, 'message': 'Return not found'}), 404
    if ret.status != 'Pending':
        return jsonify({'success': False, 'message': 'Return already processed'}), 400

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and ret.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    data   = request.json or {}
    action = data.get('action')
    if action not in ('approve', 'reject'):
        return jsonify({'success': False, 'message': 'action must be approve or reject'}), 400

    # ── REJECT ────────────────────────────────────────────────────────────────
    if action == 'reject':
        ret.status       = 'Rejected'
        ret.processed_at = datetime.utcnow()
        ret.processed_by = current_user.id
        db.session.commit()
        log_audit(current_user, 'REJECT_RETURN', 'SalesOrderReturn', return_id)
        return jsonify({'success': True,
                        'message': f'Return {ret.return_number} rejected. No inventory changes made.'})

    # ── APPROVE ───────────────────────────────────────────────────────────────
    good_count            = 0
    our_damaged_count     = 0
    customer_damaged_count = 0

    try:
        for ri in ret.items:
            stock = StockLevel.query.filter_by(
                product_id=ri.product_id,
                site_id=ret.warehouse_id
            ).with_for_update().first()

            if ri.condition == 'Good':
                # ✅ Good → restock into inventory, full refund
                if stock:
                    stock.current_quantity += ri.return_qty
                else:
                    db.session.add(StockLevel(
                        product_id       = ri.product_id,
                        site_id          = ret.warehouse_id,
                        current_quantity = ri.return_qty,
                    ))
                db.session.add(StockMovement(
                    product_id    = ri.product_id,
                    site_id       = ret.warehouse_id,
                    quantity      = ri.return_qty,
                    movement_type = 'RETURN_IN',
                    reference_id  = ret.return_number,
                    notes         = f'Good condition customer return — restocked via {ret.return_number}',
                    created_by    = current_user.username,
                ))
                good_count += 1

            elif ri.condition == 'Damaged' and ri.damage_by == 'our_damage':
                # 🏭 Our damage → restock (item is still ours) + refund customer
                if stock:
                    stock.current_quantity += ri.return_qty
                else:
                    db.session.add(StockLevel(
                        product_id       = ri.product_id,
                        site_id          = ret.warehouse_id,
                        current_quantity = ri.return_qty,
                    ))
                db.session.add(StockMovement(
                    product_id    = ri.product_id,
                    site_id       = ret.warehouse_id,
                    quantity      = ri.return_qty,
                    movement_type = 'RETURN_OUR_DAMAGE',
                    reference_id  = ret.return_number,
                    notes         = f'Our-side damage SO return via {ret.return_number} — restocked, full refund issued to customer',
                    created_by    = current_user.username,
                ))
                our_damaged_count += 1

            else:
                # 👤 Customer damage → do NOT restock, no refund (customer's fault)
                db.session.add(StockMovement(
                    product_id    = ri.product_id,
                    site_id       = ret.warehouse_id,
                    quantity      = 0,
                    movement_type = 'RETURN_CUSTOMER_DAMAGED',
                    reference_id  = ret.return_number,
                    notes         = f'Customer-damaged SO return via {ret.return_number} — not restocked, no refund',
                    created_by    = current_user.username,
                ))
                customer_damaged_count += 1

        ret.status       = 'Approved'
        ret.processed_at = datetime.utcnow()
        ret.processed_by = current_user.id
        db.session.commit()

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False,
                        'message': f'Transaction rolled back — no changes saved: {str(e)}'}), 500

    log_audit(current_user, 'APPROVE_RETURN', 'SalesOrderReturn', return_id,
              {'good': good_count, 'our_damaged': our_damaged_count,
               'customer_damaged': customer_damaged_count,
               'refund': float(ret.total_refund or 0)})

    parts = []
    if good_count:
        parts.append(f'{good_count} item(s) restocked into inventory')
    if our_damaged_count:
        parts.append(f'{our_damaged_count} item(s) restocked (our damage, refund issued)')
    if customer_damaged_count:
        parts.append(f'{customer_damaged_count} item(s) rejected (customer damage, no restock/refund)')

    return jsonify({
        'success':                True,
        'message':                f'Return {ret.return_number} approved. ' + '; '.join(parts) + '.',
        'good_count':             good_count,
        'our_damaged_count':      our_damaged_count,
        'customer_damaged_count': customer_damaged_count,
        'total_refund':           float(ret.total_refund or 0),
    })
