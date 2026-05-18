from flask import Blueprint, request, jsonify, current_app, send_file
from app.models import (PurchaseOrder, PurchaseOrderItem, StockLevel,
                        StockMovement, Inventory, Product, Site, Supplier, db)
from app.auth import token_required, log_audit
from app.dependencies import handle_exceptions
from datetime import datetime, timezone, timezone
import re, os
from app.invoice_generator import generate_purchase_invoice

bp = Blueprint('purchase_orders', __name__, url_prefix='/api/purchase-orders')


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _manager_site_ids(current_user):
    if current_user.role.role_name in ('Manager', 'Analyst') and current_user.state_id and current_user.state_id != 'ALL':
        try:
            sites = Site.query.filter_by(state_id=int(current_user.state_id)).all()
            return [s.site_id for s in sites]
        except (ValueError, TypeError):
            return []
    return None


def _format_po(po):
    site     = Site.query.get(po.warehouse_id) if po.warehouse_id else None
    supplier = Supplier.query.get(po.supplier_id) if po.supplier_id else None
    return {
        'id':                po.id,
        'po_number':         po.po_number,
        'supplier_id':       po.supplier_id,
        'supplier_name':     supplier.supplier_name if supplier else '—',
        'supplier_email':    supplier.contact_email if supplier else None,
        'warehouse_id':      po.warehouse_id,
        'warehouse_name':    site.site_name if site else po.warehouse_id,
        'order_date':        po.order_date.strftime('%d-%m-%Y') if po.order_date else '',
        'expected_delivery': po.expected_delivery.strftime('%d-%m-%Y') if po.expected_delivery else '',
        'status':            po.status,
        'total_amount':      float(po.total_amount or 0),
        'notes':             po.notes or '',
        'created_at':        po.created_at.strftime('%Y-%m-%d') if po.created_at else '',
    }


def _is_valid_email(email):
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email or ''))


# ── LIST ──────────────────────────────────────────────────────────────────────
@bp.route('', methods=['GET'])
@token_required
@handle_exceptions
def get_pos(current_user):
    page   = request.args.get('page', 1, type=int)
    search = request.args.get('search', '')
    status = request.args.get('status', '')
    site_f = request.args.get('site_id', '')

    query    = PurchaseOrder.query
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(PurchaseOrder.warehouse_id.in_(site_ids))

    if search:
        query = query.join(Supplier, PurchaseOrder.supplier_id == Supplier.supplier_id, isouter=True).filter(
            PurchaseOrder.po_number.ilike(f'%{search}%') |
            Supplier.supplier_name.ilike(f'%{search}%') |
            PurchaseOrder.warehouse_id.ilike(f'%{search}%')
        )
    if status:
        query = query.filter(PurchaseOrder.status == status)
    if site_f:
        query = query.filter(PurchaseOrder.warehouse_id == site_f)

    pagination = query.order_by(PurchaseOrder.created_at.desc()).paginate(
        page=page, per_page=15, error_out=False
    )

    return jsonify({'success': True, 'data': {
        'items':        [_format_po(p) for p in pagination.items],
        'total':        pagination.total,
        'pages':        pagination.pages,
        'current_page': page,
    }})


# ── SUMMARY ───────────────────────────────────────────────────────────────────
@bp.route('/summary', methods=['GET'])
@token_required
@handle_exceptions
def po_summary(current_user):
    query    = PurchaseOrder.query
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(PurchaseOrder.warehouse_id.in_(site_ids))

    all_pos   = query.all()
    total     = len(all_pos)
    received  = sum(1 for p in all_pos if p.status == 'Received')
    draft     = sum(1 for p in all_pos if p.status == 'Draft')
    sent      = sum(1 for p in all_pos if p.status == 'Sent')
    cancelled = sum(1 for p in all_pos if p.status == 'Cancelled')
    total_val = sum(float(p.total_amount or 0) for p in all_pos)

    return jsonify({'success': True, 'data': {
        'total_orders':     total,
        'received_orders':  received,
        'draft_orders':     draft,
        'sent_orders':      sent,
        'cancelled_orders': cancelled,
        'total_value':      round(total_val, 2),
    }})


# ── SITES ─────────────────────────────────────────────────────────────────────
@bp.route('/sites', methods=['GET'])
@token_required
@handle_exceptions
def po_sites(current_user):
    site_ids = _manager_site_ids(current_user)
    if site_ids is None:
        sites = Site.query.order_by(Site.site_name).all()
    else:
        sites = Site.query.filter(Site.site_id.in_(site_ids)).order_by(Site.site_name).all()
    return jsonify({'success': True, 'data': [
        {'site_id': s.site_id, 'site_name': s.site_name} for s in sites
    ]})


# ── SUPPLIERS ─────────────────────────────────────────────────────────────────
@bp.route('/suppliers', methods=['GET'])
@token_required
@handle_exceptions
def po_suppliers(current_user):
    suppliers = Supplier.query.filter_by(status='Active').order_by(Supplier.supplier_name).all()
    return jsonify({'success': True, 'data': [
        {
            'supplier_id':   s.supplier_id,
            'supplier_name': s.supplier_name,
            'contact_email': s.contact_email or '',
        } for s in suppliers
    ]})


# ── PRODUCTS for a supplier ───────────────────────────────────────────────────
@bp.route('/products', methods=['GET'])
@token_required
@handle_exceptions
def po_products(current_user):
    supplier_id = request.args.get('supplier_id', type=int)
    query = Product.query
    if supplier_id:
        query = query.filter(Product.supplier_id == supplier_id)
    products = query.order_by(Product.product_name).limit(200).all()
    return jsonify({'success': True, 'data': [
        {
            'product_id':   p.product_id,
            'product_name': p.product_name,
            'category':     p.category,
            'unit_cost':    float(p.unit_cost or 0),
        } for p in products
    ]})


# ── DETAIL ────────────────────────────────────────────────────────────────────
@bp.route('/<int:id>', methods=['GET'])
@token_required
@handle_exceptions
def get_po_detail(current_user, id):
    po = PurchaseOrder.query.get(id)
    if not po:
        return jsonify({'success': False, 'message': 'Not found'}), 404

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and po.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    items = []
    for item in po.items:
        prod = Product.query.get(item.product_id)
        items.append({
            'product_id':        item.product_id,
            'product_name':      prod.product_name if prod else item.product_id,
            'category':          prod.category if prod else '',
            'quantity':          item.quantity,
            'received_quantity': item.received_quantity or 0,
            'pending':           max((item.quantity or 0) - (item.received_quantity or 0), 0),
            'unit_price':        float(item.unit_price or 0),
            'line_total':        float(item.line_total or 0),
        })

    return jsonify({'success': True, 'data': {**_format_po(po), 'items': items}})


# ── CREATE ────────────────────────────────────────────────────────────────────
@bp.route('', methods=['POST'])
@token_required
@handle_exceptions
def create_po(current_user):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403
    data = request.json or {}

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and data.get('warehouse_id') not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied to this warehouse'}), 403

    if not Site.query.get(data.get('warehouse_id')):
        return jsonify({'success': False, 'message': 'Warehouse not found'}), 400

    supplier_id = data.get('supplier_id')
    if not supplier_id or not Supplier.query.get(supplier_id):
        return jsonify({'success': False, 'message': 'Valid supplier is required'}), 400

    po_num = f"PO-{datetime.now().strftime('%Y%m%d%H%M%S')}"

    order_date   = None
    exp_delivery = None
    try:
        if data.get('order_date'):
            order_date = datetime.strptime(data['order_date'], '%Y-%m-%d').date()
        if data.get('expected_delivery'):
            exp_delivery = datetime.strptime(data['expected_delivery'], '%Y-%m-%d').date()
    except ValueError:
        pass

    items_data = data.get('items', [])
    total = float(data.get('total_amount', 0))
    if not total and items_data:
        total = sum(float(i.get('unit_price', 0)) * int(i.get('quantity', 0)) for i in items_data)

    new_po = PurchaseOrder(
        po_number         = po_num,
        supplier_id       = int(supplier_id),
        warehouse_id      = data['warehouse_id'],
        order_date        = order_date or datetime.now(timezone.utc).date(),
        expected_delivery = exp_delivery,
        status            = 'Draft',
        total_amount      = total,
        notes             = data.get('notes', ''),
        created_by        = current_user.id,
    )
    db.session.add(new_po)
    db.session.flush()

    for item in items_data:
        qty   = int(item.get('quantity', 0))
        price = float(item.get('unit_price', 0))
        if qty <= 0:
            continue
        db.session.add(PurchaseOrderItem(
            po_id      = new_po.id,
            product_id = item['product_id'],
            quantity   = qty,
            unit_price = price,
            line_total = round(qty * price, 2),
        ))

    db.session.commit()

    log_audit(current_user, 'CREATE', 'PurchaseOrder', new_po.id)
    return jsonify({
        'success':     True,
        'message':     f'Purchase Order {po_num} created',
        'po_id':       new_po.id,
    })


# ── INVOICE DOWNLOAD ──────────────────────────────────────────────────────────

@bp.route('/<int:id>/invoice', methods=['GET'])
@token_required
@handle_exceptions
def download_po_invoice(current_user, id):
    """Stream a freshly generated PDF invoice — no disk I/O."""
    po = PurchaseOrder.query.get_or_404(id)
    buffer = generate_purchase_invoice(po)
    return send_file(buffer,
                     mimetype='application/pdf',
                     as_attachment=True,
                     download_name=f"{po.po_number}.pdf")


# ── SEND EMAIL ────────────────────────────────────────────────────────────────
@bp.route('/<int:id>/send-email', methods=['POST'])
@token_required
@handle_exceptions
def send_po_email(current_user, id):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403
    import sendgrid
    from sendgrid.helpers.mail import Mail as SGMail, Email
    import os

    po = PurchaseOrder.query.get(id)
    if not po:
        return jsonify({'success': False, 'message': 'PO not found'}), 404
    if po.status == 'Received':
        return jsonify({'success': False, 'message': 'Cannot resend email for a received PO'}), 400
    if po.status == 'Cancelled':
        return jsonify({'success': False, 'message': 'Cannot send email for a cancelled PO'}), 400

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and po.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    supplier = Supplier.query.get(po.supplier_id) if po.supplier_id else None
    if not supplier:
        return jsonify({'success': False, 'message': 'Supplier not found on this PO'}), 400

    data           = request.json or {}
    override_email = data.get('recipient_email', '').strip()
    recipient      = override_email or supplier.contact_email or ''

    if not _is_valid_email(recipient):
        return jsonify({
            'success':    False,
            'message':    'Supplier does not have a valid email address. Please provide one to send.',
            'needs_email': True,
        }), 400

    site             = Site.query.get(po.warehouse_id) if po.warehouse_id else None
    order_date_str   = po.order_date.strftime('%d %B %Y') if po.order_date else 'N/A'
    exp_delivery_str = po.expected_delivery.strftime('%d %B %Y') if po.expected_delivery else 'N/A'
    warehouse_name   = site.site_name if site else (po.warehouse_id or 'N/A')
    total_str        = f"Rs.{float(po.total_amount or 0):,.2f}"

    item_rows_html = ''
    item_rows_text = ''
    for item in po.items:
        name = item.product.product_name if item.product else item.product_id
        item_rows_html += f"""
        <tr>
          <td style="padding:6px 10px;border:1px solid #e2e8f0;">{name}</td>
          <td style="padding:6px 10px;border:1px solid #e2e8f0;text-align:center;">{item.quantity}</td>
          <td style="padding:6px 10px;border:1px solid #e2e8f0;text-align:right;">Rs.{float(item.unit_price or 0):,.2f}</td>
          <td style="padding:6px 10px;border:1px solid #e2e8f0;text-align:right;">Rs.{float(item.line_total or 0):,.2f}</td>
        </tr>"""
        item_rows_text += f"\n  - {name} | Qty: {item.quantity} | Unit: Rs.{float(item.unit_price or 0):,.2f} | Total: Rs.{float(item.line_total or 0):,.2f}"

    notes_html = f'<p style="background:#f8fafc;padding:12px;border-radius:6px;font-size:0.85rem;"><strong>Notes:</strong> {po.notes}</p>' if po.notes else ''

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:650px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;border-radius:8px;overflow:hidden;">
      <div style="background:#1e40af;padding:24px 30px;color:#fff;">
        <h1 style="margin:0;font-size:1.4rem;font-weight:700;">Purchase Order</h1>
        <p style="margin:4px 0 0;opacity:0.85;font-size:0.9rem;">Reference: <strong>{po.po_number}</strong></p>
      </div>
      <div style="padding:28px 30px;">
        <p style="margin:0 0 16px;">Dear <strong>{supplier.supplier_name}</strong>,</p>
        <p style="margin:0 0 20px;color:#475569;">Please find below our Purchase Order. Kindly review and confirm receipt at your earliest convenience.</p>
        <table style="width:100%;border-collapse:collapse;margin-bottom:20px;font-size:0.88rem;">
          <tr><td style="padding:6px 10px;background:#f8fafc;font-weight:600;width:40%;">PO Number</td><td style="padding:6px 10px;">{po.po_number}</td></tr>
          <tr><td style="padding:6px 10px;background:#f8fafc;font-weight:600;">Order Date</td><td style="padding:6px 10px;">{order_date_str}</td></tr>
          <tr><td style="padding:6px 10px;background:#f8fafc;font-weight:600;">Expected Delivery</td><td style="padding:6px 10px;">{exp_delivery_str}</td></tr>
          <tr><td style="padding:6px 10px;background:#f8fafc;font-weight:600;">Delivery Warehouse</td><td style="padding:6px 10px;">{warehouse_name}</td></tr>
        </table>
        <h3 style="font-size:1rem;color:#1e293b;margin:0 0 10px;border-bottom:2px solid #e2e8f0;padding-bottom:6px;">Order Items</h3>
        <table style="width:100%;border-collapse:collapse;font-size:0.85rem;margin-bottom:20px;">
          <thead><tr style="background:#f1f5f9;">
            <th style="padding:8px 10px;border:1px solid #e2e8f0;text-align:left;">Product</th>
            <th style="padding:8px 10px;border:1px solid #e2e8f0;text-align:center;">Qty</th>
            <th style="padding:8px 10px;border:1px solid #e2e8f0;text-align:right;">Unit Cost</th>
            <th style="padding:8px 10px;border:1px solid #e2e8f0;text-align:right;">Line Total</th>
          </tr></thead>
          <tbody>{item_rows_html}</tbody>
          <tfoot><tr style="background:#f8fafc;font-weight:700;">
            <td colspan="3" style="padding:8px 10px;border:1px solid #e2e8f0;text-align:right;">Grand Total</td>
            <td style="padding:8px 10px;border:1px solid #e2e8f0;text-align:right;">{total_str}</td>
          </tr></tfoot>
        </table>
        {notes_html}
        <p style="color:#64748b;font-size:0.82rem;margin-top:20px;">Please reply to this email if you have any questions regarding this order.</p>
      </div>
      <div style="background:#f1f5f9;padding:14px 30px;font-size:0.78rem;color:#94a3b8;text-align:center;">
        Automated Purchase Order notification — Inventory Management System
      </div>
    </div>"""

    text_body = f"""Purchase Order: {po.po_number}
Supplier: {supplier.supplier_name}
Order Date: {order_date_str} | Expected Delivery: {exp_delivery_str}
Warehouse: {warehouse_name}

Items:{item_rows_text}

Grand Total: {total_str}
{"Notes: " + po.notes if po.notes else ""}
"""

    try:
        sg = sendgrid.SendGridAPIClient(api_key=os.getenv('SENDGRID_API_KEY'))
        msg = SGMail(
            from_email=Email('inventoryadmin284@gmail.com', 'IFMS PRO'),
            to_emails=recipient,
            subject=f"Purchase Order {po.po_number} – {supplier.supplier_name}",
            html_content=html_body
        )
        sg.send(msg)
    except Exception as e:
        import traceback
        print("PO EMAIL ERROR:", traceback.format_exc())
        return jsonify({'success': False, 'message': f'Email send failed: {str(e)}'}), 500

    # Persist email on supplier for future use if it was an override
    if override_email and not supplier.contact_email:
        supplier.contact_email = override_email

    if po.status == 'Draft':
        po.status = 'Sent'
    db.session.commit()

    log_audit(current_user, 'SEND_EMAIL', 'PurchaseOrder', id)
    return jsonify({
        'success':   True,
        'message':   f'Email sent to {recipient}. PO status updated to Sent.',
        'recipient': recipient,
        'status':    po.status,
    })


# ── RECEIVE ───────────────────────────────────────────────────────────────────
@bp.route('/<int:id>/receive', methods=['POST'])
@token_required
@handle_exceptions
def receive_po(current_user, id):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403
    po = PurchaseOrder.query.get(id)
    if not po:
        return jsonify({'success': False, 'message': 'PO not found'}), 404
    if po.status == 'Received':
        return jsonify({'success': False, 'message': 'PO already received. Duplicate receive prevented.'}), 400
    if po.status == 'Cancelled':
        return jsonify({'success': False, 'message': 'Cannot receive a cancelled PO'}), 400

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and po.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    data           = request.json or {}
    item_overrides = {i['product_id']: int(i['received_qty'])
                      for i in data.get('items', []) if 'received_qty' in i}

    try:
        for item in po.items:
            recv_qty = item_overrides.get(item.product_id, item.quantity)
            if recv_qty <= 0:
                continue

            # 1. StockLevel — running balance
            stock = StockLevel.query.filter_by(
                product_id=item.product_id,
                site_id=po.warehouse_id
            ).with_for_update().first()

            if stock:
                stock.current_quantity += recv_qty
            else:
                db.session.add(StockLevel(
                    product_id       = item.product_id,
                    site_id          = po.warehouse_id,
                    current_quantity = recv_qty,
                ))

            # 2. Inventory — snapshot record (transactional, one per receive)
            prev_inv = Inventory.query.filter_by(
                product_id = item.product_id,
                site_id    = po.warehouse_id,
            ).order_by(Inventory.created_at.desc()).first()

            beginning     = int(prev_inv.ending_inventory or 0) if prev_inv else 0
            replenishment = recv_qty
            ending        = beginning + replenishment
            stockout_flag = 'No' if ending > 0 else 'Yes'

            db.session.add(Inventory(
                site_id             = po.warehouse_id,
                product_id          = item.product_id,
                beginning_inventory = beginning,
                replenishment       = replenishment,
                ending_inventory    = ending,
                stockout_flag       = stockout_flag,
            ))

            # 3. Movement log
            db.session.add(StockMovement(
                product_id    = item.product_id,
                site_id       = po.warehouse_id,
                quantity      = recv_qty,
                movement_type = 'PURCHASE_IN',
                reference_id  = po.po_number,
                notes         = f'Received via {po.po_number}',
                created_by    = current_user.username,
            ))

            item.received_quantity = recv_qty

        po.status = 'Received'
        db.session.commit()

    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False,
                        'message': f'Transaction rolled back — no changes saved: {str(e)}'}), 500

    log_audit(current_user, 'RECEIVE', 'PurchaseOrder', id)
    return jsonify({'success': True,
                    'message': f'PO {po.po_number} received — stock levels and inventory updated.'})


# ── CANCEL ────────────────────────────────────────────────────────────────────
@bp.route('/<int:id>/cancel', methods=['POST'])
@token_required
@handle_exceptions
def cancel_po(current_user, id):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403
    po = PurchaseOrder.query.get(id)
    if not po:
        return jsonify({'success': False, 'message': 'PO not found'}), 404
    if po.status not in ('Draft', 'Sent'):
        return jsonify({'success': False, 'message': 'Only Draft or Sent POs can be cancelled'}), 400

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and po.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    po.status = 'Cancelled'
    db.session.commit()
    log_audit(current_user, 'CANCEL', 'PurchaseOrder', id)
    return jsonify({'success': True, 'message': f'PO {po.po_number} cancelled'})
