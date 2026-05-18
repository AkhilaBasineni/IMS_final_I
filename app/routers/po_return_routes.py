from flask import Blueprint, request, jsonify
from app.models import (PurchaseOrder, PurchaseOrderItem, PurchaseOrderReturn,
                        PurchaseOrderReturnItem, StockLevel, StockMovement,
                        Product, Site, Supplier, db)
from app.auth import token_required, log_audit
from app.dependencies import handle_exceptions
from datetime import datetime

bp_po_returns = Blueprint('po_returns', __name__, url_prefix='/api/purchase-orders')


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
    po       = PurchaseOrder.query.get(ret.po_id)
    supplier = Supplier.query.get(po.supplier_id) if po and po.supplier_id else None
    site     = Site.query.get(ret.warehouse_id) if ret.warehouse_id else None
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
        'po_id':          ret.po_id,
        'po_number':      po.po_number if po else '—',
        'supplier_name':  supplier.supplier_name if supplier else '—',
        'warehouse_id':   ret.warehouse_id,
        'warehouse_name': site.site_name if site else ret.warehouse_id,
        'status':         ret.status,
        'total_credit':   float(ret.total_credit or 0),
        'notes':          ret.notes or '',
        'created_at':     ret.created_at.strftime('%Y-%m-%d') if ret.created_at else '',
        'processed_at':   ret.processed_at.strftime('%Y-%m-%d %H:%M') if ret.processed_at else None,
        'items':          items,
    }


# ── LIST ALL PO RETURNS (global, paginated) ───────────────────────────────────
@bp_po_returns.route('/returns', methods=['GET'])
@token_required
@handle_exceptions
def list_all_po_returns(current_user):
    page      = request.args.get('page', 1, type=int)
    status    = request.args.get('status', '')
    search    = request.args.get('search', '').strip()
    warehouse = request.args.get('warehouse', '').strip()

    query    = PurchaseOrderReturn.query
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(PurchaseOrderReturn.warehouse_id.in_(site_ids))

    if status:
        query = query.filter(PurchaseOrderReturn.status == status)

    if search:
        query = query.outerjoin(PurchaseOrder, PurchaseOrderReturn.po_id == PurchaseOrder.id)
        query = query.filter(
            db.or_(
                PurchaseOrderReturn.return_number.ilike(f'%{search}%'),
                PurchaseOrder.po_number.ilike(f'%{search}%'),
            )
        )

    if warehouse:
        site = Site.query.filter(Site.site_name.ilike(warehouse)).first()
        if site:
            query = query.filter(PurchaseOrderReturn.warehouse_id == site.site_id)
        else:
            query = query.filter(db.false())

    pagination = query.order_by(PurchaseOrderReturn.created_at.desc()).paginate(
        page=page, per_page=15, error_out=False)

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


# ── RETURNS FOR ONE PO ────────────────────────────────────────────────────────
@bp_po_returns.route('/<int:po_id>/returns', methods=['GET'])
@token_required
@handle_exceptions
def list_po_returns(current_user, po_id):
    po = PurchaseOrder.query.get(po_id)
    if not po:
        return jsonify({'success': False, 'message': 'Purchase Order not found'}), 404
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and po.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    returns = PurchaseOrderReturn.query.filter_by(po_id=po_id)\
        .order_by(PurchaseOrderReturn.created_at.desc()).all()
    return jsonify({'success': True, 'data': [_format_return(r) for r in returns]})


# ── SUMMARY KPIs ──────────────────────────────────────────────────────────────
@bp_po_returns.route('/returns/summary', methods=['GET'])
@token_required
@handle_exceptions
def po_returns_summary(current_user):
    query    = PurchaseOrderReturn.query
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(PurchaseOrderReturn.warehouse_id.in_(site_ids))
    all_returns = query.all()
    return jsonify({'success': True, 'data': {
        'total_returns': len(all_returns),
        'pending':       sum(1 for r in all_returns if r.status == 'Pending'),
        'approved':      sum(1 for r in all_returns if r.status == 'Approved'),
        'rejected':      sum(1 for r in all_returns if r.status == 'Rejected'),
        'total_credit':  round(sum(float(r.total_credit or 0)
                                   for r in all_returns if r.status == 'Approved'), 2),
    }})


# ── INITIATE RETURN ───────────────────────────────────────────────────────────
@bp_po_returns.route('/<int:po_id>/returns', methods=['POST'])
@token_required
@handle_exceptions
def create_po_return(current_user, po_id):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403

    po = PurchaseOrder.query.get(po_id)
    if not po:
        return jsonify({'success': False, 'message': 'Purchase Order not found'}), 404
    if po.status != 'Received':
        return jsonify({'success': False,
                        'message': f'Only Received POs can be returned. '
                                   f'Current status: {po.status}'}), 400

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and po.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    data       = request.json or {}
    items_data = data.get('items', [])
    if not items_data:
        return jsonify({'success': False, 'message': 'At least one item is required'}), 400

    # received qty per product
    received_map = {item.product_id: (item.received_quantity or item.quantity or 0)
                    for item in po.items}

    # already approved returns for this PO
    already_returned = {}
    for prev in PurchaseOrderReturn.query.filter_by(po_id=po_id, status='Approved').all():
        for ri in prev.items:
            already_returned[ri.product_id] = already_returned.get(ri.product_id, 0) + ri.return_qty

    # validate each item
    for ri in items_data:
        pid     = ri.get('product_id')
        ret_qty = int(ri.get('return_qty', 0))
        if ret_qty <= 0:
            return jsonify({'success': False,
                            'message': f'Return quantity must be > 0 for {pid}'}), 400
        received   = received_map.get(pid, 0)
        done       = already_returned.get(pid, 0)
        returnable = received - done
        if ret_qty > returnable:
            prod = Product.query.get(pid)
            name = prod.product_name if prod else pid
            return jsonify({'success': False,
                            'message': f'Cannot return {ret_qty} of "{name}". '
                                       f'Received: {received}, Already returned: {done}, '
                                       f'Returnable: {returnable}'}), 400
        if ri.get('condition') not in ('Good', 'Damaged'):
            return jsonify({'success': False,
                            'message': f'Condition must be Good or Damaged for {pid}'}), 400
        if ri.get('condition') == 'Damaged' and \
                ri.get('damage_by') not in ('supplier_damage', 'our_damage'):
            return jsonify({'success': False,
                            'message': f'For damaged items, damage_by must be "supplier_damage" or "our_damage" for {pid}'}), 400

    ret_num      = f"PORET-{po.po_number}-{datetime.now().strftime('%H%M%S')}"
    total_credit = 0.0

    for ri in items_data:
        pid       = ri.get('product_id')
        qty       = int(ri.get('return_qty', 0))
        cond      = ri.get('condition', 'Good')
        damage_by = ri.get('damage_by', '')
        po_item   = next((i for i in po.items if i.product_id == pid), None)
        unit_price = float(po_item.unit_price or 0) if po_item else 0.0
        # Credit issued for Good items AND supplier-damaged items
        if cond == 'Good' or (cond == 'Damaged' and damage_by == 'supplier_damage'):
            total_credit += qty * unit_price

    new_ret = PurchaseOrderReturn(
        return_number = ret_num,
        po_id         = po_id,
        warehouse_id  = po.warehouse_id,
        status        = 'Pending',
        total_credit  = round(total_credit, 2),
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
        po_item    = next((i for i in po.items if i.product_id == pid), None)
        unit_price = float(po_item.unit_price or 0) if po_item else 0.0
        # Credit for Good + supplier_damage; no credit for our_damage
        has_credit = (cond == 'Good') or (cond == 'Damaged' and damage_by == 'supplier_damage')
        line_total = round(qty * unit_price, 2) if has_credit else 0.0

        db.session.add(PurchaseOrderReturnItem(
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
    log_audit(current_user, 'CREATE_RETURN', 'PurchaseOrderReturn', new_ret.id,
              {'return_number': ret_num, 'po_number': po.po_number})

    return jsonify({
        'success':       True,
        'message':       f'Return {ret_num} initiated. Pending inspection.',
        'return_id':     new_ret.id,
        'return_number': ret_num,
        'total_credit':  float(new_ret.total_credit),
    })


# ── PROCESS RETURN (Approve / Reject) ────────────────────────────────────────
@bp_po_returns.route('/returns/<int:return_id>/process', methods=['POST'])
@token_required
@handle_exceptions
def process_po_return(current_user, return_id):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403

    ret = PurchaseOrderReturn.query.get(return_id)
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
        log_audit(current_user, 'REJECT_RETURN', 'PurchaseOrderReturn', return_id)
        return jsonify({'success': True,
                        'message': f'Return {ret.return_number} rejected. No inventory changes made.'})

    # ── APPROVE ───────────────────────────────────────────────────────────────
    good_count             = 0
    supplier_damaged_count = 0
    our_damaged_count      = 0

    try:
        for ri in ret.items:
            stock = StockLevel.query.filter_by(
                product_id=ri.product_id,
                site_id=ret.warehouse_id
            ).with_for_update().first()

            if ri.condition == 'Good':
                # Good → deduct from inventory (supplier takes back), credit issued
                if stock and stock.current_quantity >= ri.return_qty:
                    stock.current_quantity -= ri.return_qty
                elif stock:
                    stock.current_quantity = 0
                db.session.add(StockMovement(
                    product_id    = ri.product_id,
                    site_id       = ret.warehouse_id,
                    quantity      = -ri.return_qty,
                    movement_type = 'PO_RETURN_OUT',
                    reference_id  = ret.return_number,
                    notes         = f'Good condition PO return — sent back to supplier via {ret.return_number}',
                    created_by    = current_user.username,
                ))
                good_count += 1

            elif ri.condition == 'Damaged' and ri.damage_by == 'supplier_damage':
                # Supplier damaged → deduct from inventory + credit issued (supplier pays)
                if stock and stock.current_quantity >= ri.return_qty:
                    stock.current_quantity -= ri.return_qty
                elif stock:
                    stock.current_quantity = 0
                db.session.add(StockMovement(
                    product_id    = ri.product_id,
                    site_id       = ret.warehouse_id,
                    quantity      = -ri.return_qty,
                    movement_type = 'PO_RETURN_SUPPLIER_DAMAGED',
                    reference_id  = ret.return_number,
                    notes         = f'Supplier-damaged PO return via {ret.return_number} — deducted, credit claimed from supplier',
                    created_by    = current_user.username,
                ))
                supplier_damaged_count += 1

            else:
                # Our damage → write off only, no credit, no return to supplier
                if stock and stock.current_quantity >= ri.return_qty:
                    stock.current_quantity -= ri.return_qty
                elif stock:
                    stock.current_quantity = 0
                db.session.add(StockMovement(
                    product_id    = ri.product_id,
                    site_id       = ret.warehouse_id,
                    quantity      = -ri.return_qty,
                    movement_type = 'PO_RETURN_OUR_DAMAGE',
                    reference_id  = ret.return_number,
                    notes         = f'Our-side damage via {ret.return_number} — written off, no credit claimed',
                    created_by    = current_user.username,
                ))
                our_damaged_count += 1

        ret.status       = 'Approved'
        ret.processed_at = datetime.utcnow()
        ret.processed_by = current_user.id
        db.session.commit()

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False,
                        'message': f'Transaction rolled back — no changes saved: {str(e)}'}), 500

    log_audit(current_user, 'APPROVE_RETURN', 'PurchaseOrderReturn', return_id,
              {'good': good_count, 'supplier_damaged': supplier_damaged_count,
               'our_damaged': our_damaged_count,
               'credit': float(ret.total_credit or 0)})

    parts = []
    if good_count:
        parts.append(f'{good_count} item(s) deducted from inventory (returned to supplier)')
    if supplier_damaged_count:
        parts.append(f'{supplier_damaged_count} item(s) deducted — supplier-damaged, credit claimed')
    if our_damaged_count:
        parts.append(f'{our_damaged_count} item(s) written off (our damage, no credit)')

    return jsonify({
        'success':               True,
        'message':               f'Return {ret.return_number} approved. ' + '; '.join(parts) + '.',
        'good_count':            good_count,
        'supplier_damaged_count': supplier_damaged_count,
        'our_damaged_count':     our_damaged_count,
        'total_credit':          float(ret.total_credit or 0),
    })
