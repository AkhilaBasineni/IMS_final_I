from flask import Blueprint, request, jsonify
from app.models import Supplier, PurchaseOrder, Product, db
from app.auth import token_required, role_required, log_audit
from app.dependencies import handle_exceptions
from sqlalchemy import func, or_

bp_suppliers = Blueprint('suppliers', __name__, url_prefix='/api/suppliers')


def _format_supplier(s, include_stats=False):
    data = {
        'supplier_id':   s.supplier_id,
        'supplier_name': s.supplier_name,
        'contact_email': s.contact_email or '',
        'status':        s.status or 'Active',
        'created_at':    s.created_at.strftime('%Y-%m-%d') if s.created_at else '',
    }
    if include_stats:
        stats = db.session.query(
            func.count(PurchaseOrder.id).label('total_orders'),
            func.coalesce(func.sum(PurchaseOrder.total_amount), 0).label('total_value'),
        ).filter(PurchaseOrder.supplier_id == s.supplier_id).first()

        status_counts = db.session.query(
            PurchaseOrder.status,
            func.count(PurchaseOrder.id)
        ).filter(PurchaseOrder.supplier_id == s.supplier_id).group_by(PurchaseOrder.status).all()

        product_count = Product.query.filter_by(supplier_id=s.supplier_id).count()

        data['stats'] = {
            'total_orders':   int(stats.total_orders or 0),
            'total_value':    float(stats.total_value or 0),
            'product_count':  product_count,
            'by_status':      {st: cnt for st, cnt in status_counts},
        }
    return data


# ── SUMMARY ───────────────────────────────────────────────────────────────────
@bp_suppliers.route('/summary', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def supplier_summary(current_user):
    total  = Supplier.query.count()
    active = Supplier.query.filter_by(status='Active').count()
    inactive = Supplier.query.filter_by(status='Inactive').count()

    total_po_val = db.session.query(
        func.coalesce(func.sum(PurchaseOrder.total_amount), 0)
    ).scalar()

    return jsonify({'success': True, 'data': {
        'total_suppliers':    total,
        'active_suppliers':   active,
        'inactive_suppliers': inactive,
        'total_po_value':     float(total_po_val or 0),
    }})


# ── LIST ──────────────────────────────────────────────────────────────────────
@bp_suppliers.route('', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_suppliers(current_user):
    page   = request.args.get('page', 1, type=int)
    search = request.args.get('search', '').strip()
    status = request.args.get('status', '').strip()

    query = Supplier.query

    if search:
        query = query.filter(or_(
            Supplier.supplier_name.ilike(f'%{search}%'),
            Supplier.contact_email.ilike(f'%{search}%'),
        ))
    if status:
        query = query.filter(Supplier.status == status)

    pagination = query.order_by(Supplier.supplier_name).paginate(
        page=page, per_page=15, error_out=False
    )

    return jsonify({'success': True, 'data': {
        'items':        [_format_supplier(s) for s in pagination.items],
        'total':        pagination.total,
        'pages':        pagination.pages,
        'current_page': page,
    }})


# ── DETAIL ────────────────────────────────────────────────────────────────────
@bp_suppliers.route('/<int:supplier_id>', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_supplier(current_user, supplier_id):
    s = Supplier.query.get(supplier_id)
    if not s:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404

    # Recent purchase orders
    recent_pos = PurchaseOrder.query.filter_by(supplier_id=supplier_id)\
        .order_by(PurchaseOrder.created_at.desc()).limit(10).all()

    po_list = [{
        'id':          po.id,
        'po_number':   po.po_number,
        'status':      po.status,
        'total_amount': float(po.total_amount or 0),
        'order_date':  po.order_date.strftime('%d-%m-%Y') if po.order_date else '',
    } for po in recent_pos]

    # Products supplied
    products = Product.query.filter_by(supplier_id=supplier_id)\
        .order_by(Product.product_name).limit(50).all()

    prod_list = [{
        'product_id':   p.product_id,
        'product_name': p.product_name,
        'category':     p.category or '',
        'unit_cost':    float(p.unit_cost or 0),
    } for p in products]

    data = _format_supplier(s, include_stats=True)
    data['recent_orders'] = po_list
    data['products']      = prod_list
    return jsonify({'success': True, 'data': data})


# ── CREATE ────────────────────────────────────────────────────────────────────
@bp_suppliers.route('', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def create_supplier(current_user):
    data = request.get_json() or {}

    name = (data.get('supplier_name') or '').strip()
    if not name:
        return jsonify({'success': False, 'message': 'supplier_name is required'}), 400

    if Supplier.query.filter(Supplier.supplier_name.ilike(name)).first():
        return jsonify({'success': False, 'message': f'Supplier "{name}" already exists'}), 400

    s = Supplier(
        supplier_name = name,
        contact_email = (data.get('contact_email') or '').strip() or None,
        status        = data.get('status', 'Active'),
    )
    db.session.add(s)
    db.session.commit()
    log_audit(current_user, 'CREATE', 'Supplier', s.supplier_id)
    return jsonify({
        'success': True,
        'data':    _format_supplier(s),
        'message': f'Supplier "{name}" created successfully',
    }), 201


# ── UPDATE ────────────────────────────────────────────────────────────────────
@bp_suppliers.route('/<int:supplier_id>', methods=['PUT'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def update_supplier(current_user, supplier_id):
    s = Supplier.query.get(supplier_id)
    if not s:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404

    data = request.get_json() or {}

    if 'supplier_name' in data:
        name = (data['supplier_name'] or '').strip()
        if not name:
            return jsonify({'success': False, 'message': 'supplier_name cannot be empty'}), 400
        # Check uniqueness (exclude self)
        existing = Supplier.query.filter(
            Supplier.supplier_name.ilike(name),
            Supplier.supplier_id != supplier_id
        ).first()
        if existing:
            return jsonify({'success': False, 'message': f'Supplier "{name}" already exists'}), 400
        s.supplier_name = name

    if 'contact_email' in data:
        s.contact_email = (data['contact_email'] or '').strip() or None
    if 'status' in data:
        if data['status'] not in ('Active', 'Inactive'):
            return jsonify({'success': False, 'message': 'status must be Active or Inactive'}), 400
        s.status = data['status']

    db.session.commit()
    log_audit(current_user, 'UPDATE', 'Supplier', supplier_id)
    return jsonify({'success': True, 'data': _format_supplier(s), 'message': 'Supplier updated successfully'})


# ── DELETE ────────────────────────────────────────────────────────────────────
@bp_suppliers.route('/<int:supplier_id>', methods=['DELETE'])
@token_required
@role_required('Admin')
@handle_exceptions
def delete_supplier(current_user, supplier_id):
    s = Supplier.query.get(supplier_id)
    if not s:
        return jsonify({'success': False, 'message': 'Supplier not found'}), 404

    # Safety: block delete if POs exist
    po_count = PurchaseOrder.query.filter_by(supplier_id=supplier_id).count()
    if po_count > 0:
        return jsonify({
            'success': False,
            'message': f'Cannot delete: {po_count} purchase order(s) are linked to this supplier. '
                       f'Set status to Inactive instead.'
        }), 400

    # Nullify product FK references before deleting
    Product.query.filter_by(supplier_id=supplier_id).update({'supplier_id': None})
    db.session.flush()
    db.session.delete(s)
    db.session.commit()
    log_audit(current_user, 'DELETE', 'Supplier', supplier_id)
    return jsonify({'success': True, 'message': f'Supplier "{s.supplier_name}" deleted'})
