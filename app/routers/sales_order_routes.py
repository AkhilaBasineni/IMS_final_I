from flask import Blueprint, request, jsonify, current_app, send_file
from app.models import (SalesOrder, SalesOrderItem, StockLevel, StockMovement,
                        Product, Site, Customer, Logistics, Sale, Promotion, db)
from app.auth import token_required, log_audit
from app.dependencies import handle_exceptions
from datetime import datetime, timezone
import re, threading, os, csv
from app.invoice_generator import generate_sales_invoice
from sqlalchemy.exc import IntegrityError

bp = Blueprint('sales_orders', __name__, url_prefix='/api/sales-orders')

# ── HELPERS ───────────────────────────────────────────────────────────────────

def _is_valid_email(email):
    return bool(re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email or ''))


def _manager_site_ids(current_user):
    if current_user.role.role_name in ('Manager', 'Analyst') and current_user.state_id and current_user.state_id != 'ALL':
        try:
            sites = Site.query.filter_by(state_id=int(current_user.state_id)).all()
            return [s.site_id for s in sites]
        except (ValueError, TypeError):
            return []
    return None


_so_number_lock = threading.Lock()

_so_number_lock = threading.Lock()

def _generate_so_number():
    with _so_number_lock:
        last = db.session.query(db.func.max(SalesOrder.id)).scalar()

        if last:
            last_order = SalesOrder.query.get(last)
            if last_order and last_order.so_number:
                try:
                    num = int(last_order.so_number.split('-')[1]) + 1
                except (IndexError, ValueError):
                    num = 10001
            else:
                num = 10001
        else:
            num = 10001

        return f"SO-{num}"

def _format_so(so):
    site = so.warehouse
    cust = so.customer
    product_ids = [item.product_id for item in so.items] if so.items else []
    return {
        'id':             so.id,
        'so_number':      so.so_number,
        'customer_id':    so.customer_id,
        'customer_name':  (cust.name if cust and cust.name else None) or so.customer_id or 'Walk-in',
        'customer_email': cust.email if cust and cust.email else '',
        'warehouse_id':   so.warehouse_id,
        'warehouse_name': site.site_name if site else so.warehouse_id,
        'order_date':     so.order_date.strftime('%d-%m-%Y') if so.order_date else '',
        'status':         so.status,
        'total_amount':   float(so.total_amount or 0),
        'discount':       float(so.discount or 0),
        'notes':          so.notes or '',
        'transport':      so.transport or '',
        'email_sent':     bool(so.email_sent),
        'created_at':     so.created_at.strftime('%Y-%m-%d %H:%M') if so.created_at else '',
        'product_ids':    product_ids,
    }


def _build_email_html(so, items_data, status_label='Confirmed'):
    site = so.warehouse
    cust = so.customer
    customer_name  = (cust.name  if cust and cust.name  else so.customer_id) or 'Valued Customer'
    customer_email = (cust.email if cust and cust.email else '') or '—'
    warehouse_name = site.site_name if site else (so.warehouse_id or '—')
    order_date     = so.order_date.strftime('%d %b %Y') if so.order_date else '—'

    rows = ''
    for item in items_data:
        rows += f"""
        <tr>
          <td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;">{item['product_name']}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;text-align:center;">{item['quantity']}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;text-align:right;">&#8377;{item['unit_price']:,.2f}</td>
          <td style="padding:10px 14px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:600;">&#8377;{item['line_total']:,.2f}</td>
        </tr>"""

    if status_label == 'Shipped':
        status_color, icon = '#10b981', '&#128666;'
        headline   = 'Your Order Has Been Dispatched!'
        subline    = 'Your order is on its way. Thank you for your business.'
    else:
        status_color, icon = '#4f46e5', '&#9989;'
        headline   = 'Sales Order Confirmed'
        subline    = 'Your order has been confirmed and will be dispatched shortly.'

    notes_block = f'<p style="margin-top:16px;color:#64748b;font-size:0.85rem;">&#128221; Notes: {so.notes}</p>' if so.notes else ''

    return f"""
    <div style="font-family:Arial,sans-serif;max-width:680px;margin:0 auto;background:#f8fafc;">
      <div style="background:linear-gradient(135deg,#0f172a,#4f46e5);padding:36px 32px;border-radius:16px 16px 0 0;text-align:center;">
        <div style="font-size:2.5rem;">{icon}</div>
        <h2 style="color:white;margin:12px 0 4px;font-size:1.5rem;">{headline}</h2>
        <p style="color:#c7d2fe;margin:0;font-size:0.9rem;">{subline}</p>
      </div>
      <div style="background:white;padding:32px;border:1px solid #e2e8f0;">
        <table style="width:100%;margin-bottom:24px;">
          <tr><td style="padding:6px 0;color:#64748b;font-size:0.85rem;">SO Number</td><td style="text-align:right;font-weight:700;color:#4f46e5;">{so.so_number}</td></tr>
          <tr><td style="padding:6px 0;color:#64748b;font-size:0.85rem;">Order Date</td><td style="text-align:right;font-weight:600;">{order_date}</td></tr>
          <tr><td style="padding:6px 0;color:#64748b;font-size:0.85rem;">Status</td>
              <td style="text-align:right;"><span style="background:{status_color};color:white;padding:3px 12px;border-radius:20px;font-size:0.78rem;font-weight:700;">{status_label}</span></td></tr>
          <tr><td style="padding:6px 0;color:#64748b;font-size:0.85rem;">Warehouse</td><td style="text-align:right;font-weight:600;">{warehouse_name}</td></tr>
        </table>
        <div style="background:#f1f5f9;border-radius:10px;padding:16px 20px;margin-bottom:24px;">
          <h4 style="margin:0 0 10px;color:#0f172a;font-size:0.95rem;">Customer Details</h4>
          <p style="margin:4px 0;color:#475569;font-size:0.87rem;">&#128100; <strong>{customer_name}</strong></p>
          <p style="margin:4px 0;color:#475569;font-size:0.87rem;">&#9993; {customer_email}</p>
        </div>
        <h4 style="margin:0 0 12px;color:#0f172a;font-size:0.95rem;">Order Items</h4>
        <table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
          <thead><tr style="background:#f8fafc;">
            <th style="padding:10px 14px;text-align:left;font-size:0.82rem;color:#64748b;border-bottom:2px solid #e2e8f0;">Product</th>
            <th style="padding:10px 14px;text-align:center;font-size:0.82rem;color:#64748b;border-bottom:2px solid #e2e8f0;">Qty</th>
            <th style="padding:10px 14px;text-align:right;font-size:0.82rem;color:#64748b;border-bottom:2px solid #e2e8f0;">Unit Price</th>
            <th style="padding:10px 14px;text-align:right;font-size:0.82rem;color:#64748b;border-bottom:2px solid #e2e8f0;">Total</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <div style="text-align:right;border-top:2px solid #e2e8f0;padding-top:16px;">
          <span style="font-size:1.1rem;color:#0f172a;">Grand Total: </span>
          <span style="font-size:1.4rem;font-weight:800;color:#4f46e5;">&#8377;{float(so.total_amount or 0):,.2f}</span>
        </div>
        {notes_block}
      </div>
      <div style="background:#1e293b;padding:16px;border-radius:0 0 16px 16px;text-align:center;">
        <p style="color:#64748b;font-size:0.78rem;margin:0;">&#169; 2024 InventoHub &middot; support@ifmspro.com</p>
      </div>
    </div>"""


def _send_async(app, subject, recipients, html_body):
    def _go():
        with app.app_context():
            try:
                import sendgrid
                from sendgrid.helpers.mail import Mail as SGMail, Email
                import os
                sg = sendgrid.SendGridAPIClient(api_key=os.getenv('SENDGRID_API_KEY'))
                for recipient in recipients:
                    msg = SGMail(
                        from_email=Email('inventoryadmin284@gmail.com', 'IFMS PRO'),
                        to_emails=recipient,
                        subject=subject,
                        html_content=html_body
                    )
                    sg.send(msg)
            except Exception as e:
                import traceback
                print(f'[SO EMAIL ERROR] {traceback.format_exc()}')
    threading.Thread(target=_go, daemon=True).start()


# ── LIST ──────────────────────────────────────────────────────────────────────

@bp.route('', methods=['GET'])
@token_required
@handle_exceptions
def get_sales_orders(current_user):
    page        = request.args.get('page', 1, type=int)
    per_page    = request.args.get('per_page', 15, type=int)
    per_page    = min(per_page, 9999)   # cap to prevent abuse
    search      = request.args.get('search', '')
    status      = request.args.get('status', '')
    site_f      = request.args.get('site_id', '')
    customer_f  = request.args.get('customer_id', '')

    query    = SalesOrder.query
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(SalesOrder.warehouse_id.in_(site_ids))
    if search:
        query = query.filter(
            SalesOrder.so_number.ilike(f'%{search}%') |
            SalesOrder.customer_id.ilike(f'%{search}%') |
            SalesOrder.warehouse_id.ilike(f'%{search}%')
        )
    if status:
        query = query.filter(SalesOrder.status == status)
    if site_f:
        query = query.filter(SalesOrder.warehouse_id == site_f)
    if customer_f:
        query = query.filter(SalesOrder.customer_id == customer_f)

    pagination = query.order_by(SalesOrder.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False)

    return jsonify({'success': True, 'data': {
        'items':        [_format_so(so) for so in pagination.items],
        'total':        pagination.total,
        'pages':        pagination.pages,
        'current_page': page,
    }})


# ── SUMMARY ───────────────────────────────────────────────────────────────────

@bp.route('/summary', methods=['GET'])
@token_required
@handle_exceptions
def so_summary(current_user):
    query    = SalesOrder.query
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(SalesOrder.warehouse_id.in_(site_ids))
    all_sos = query.all()
    return jsonify({'success': True, 'data': {
        'total_orders':     len(all_sos),
        'shipped_orders':   sum(1 for s in all_sos if s.status == 'Shipped'),
        'delivered_orders': sum(1 for s in all_sos if s.status == 'Delivered'),
        'confirmed_orders': sum(1 for s in all_sos if s.status == 'Confirmed'),
        'draft_orders':     sum(1 for s in all_sos if s.status == 'Draft'),
        'cancelled_orders': sum(1 for s in all_sos if s.status == 'Cancelled'),
        'total_revenue':    round(sum(float(s.total_amount or 0) for s in all_sos if s.status != 'Cancelled'), 2),
    }})


# ── DETAIL ────────────────────────────────────────────────────────────────────

@bp.route('/<int:id>', methods=['GET'])
@token_required
@handle_exceptions
def get_so_detail(current_user, id):
    so = SalesOrder.query.get(id)
    if not so:
        return jsonify({'success': False, 'message': 'Sales Order not found'}), 404
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and so.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    items = []
    for item in so.items:
        prod  = item.product
        stock = StockLevel.query.filter_by(product_id=item.product_id, site_id=so.warehouse_id).first()
        items.append({
            'id':               item.id,
            'product_id':       item.product_id,
            'product_name':     prod.product_name if prod else item.product_id,
            'category':         prod.category if prod else '',
            'quantity':         item.quantity,
            'shipped_quantity': item.shipped_quantity or 0,
            'unit_price':       float(item.unit_price or 0),
            'line_total':       float(item.line_total or 0),
            'stock_available':  stock.current_quantity if stock else 0,
        })
    return jsonify({'success': True, 'data': {**_format_so(so), 'items': items}})


# ── SITES ─────────────────────────────────────────────────────────────────────

@bp.route('/sites', methods=['GET'])
@token_required
@handle_exceptions
def so_sites(current_user):
    site_ids = _manager_site_ids(current_user)
    if site_ids is None:
        sites = Site.query.order_by(Site.site_name).all()
    else:
        sites = Site.query.filter(Site.site_id.in_(site_ids)).order_by(Site.site_name).all()
    return jsonify({'success': True, 'data': [
        {'site_id': s.site_id, 'site_name': s.site_name} for s in sites]})


# ── CUSTOMERS ─────────────────────────────────────────────────────────────────

@bp.route('/customers', methods=['GET'])
@token_required
@handle_exceptions
def so_customers(current_user):
    q = request.args.get('q', '').strip()
    query = Customer.query
    if q:
        query = query.filter(
            Customer.customer_id.ilike(f'%{q}%') |
            Customer.name.ilike(f'%{q}%') |
            Customer.email.ilike(f'%{q}%')
        )
    customers = query.order_by(Customer.customer_id).limit(200).all()
    return jsonify({'success': True, 'data': [{
        'customer_id': c.customer_id,
        'name':        c.name  or c.customer_id,
        'email':       c.email or '',
    } for c in customers]})


# ── PRODUCTS with stock ───────────────────────────────────────────────────────

@bp.route('/products', methods=['GET'])
@token_required
@handle_exceptions
def so_products(current_user):
    site_id  = request.args.get('site_id', '').strip()
    q        = request.args.get('q', '').strip()
    query    = Product.query.filter_by(status='Active')
    if q:
        query = query.filter(
            Product.product_name.ilike(f'%{q}%') |
            Product.product_id.ilike(f'%{q}%')
        )
    products = query.order_by(Product.product_name).limit(300).all()
    result   = []
    for p in products:
        stock_qty = 0
        if site_id:
            sl = StockLevel.query.filter_by(product_id=p.product_id, site_id=site_id).first()
            stock_qty = sl.current_quantity if sl else 0
        result.append({
            'product_id':   p.product_id,
            'product_name': p.product_name,
            'category':     p.category or '',
            'unit_price':   float(p.unit_price or 0),
            'stock_qty':    stock_qty,
        })
    return jsonify({'success': True, 'data': result})


# ── STOCK CHECK ───────────────────────────────────────────────────────────────

@bp.route('/stock-check', methods=['POST'])
@token_required
@handle_exceptions
def stock_check(current_user):
    data    = request.json or {}
    site_id = data.get('site_id', '')
    items   = data.get('items', [])
    result  = []
    for item in items:
        stock = StockLevel.query.filter_by(product_id=item['product_id'], site_id=site_id).first()
        avail = stock.current_quantity if stock else 0
        result.append({
            'product_id': item['product_id'],
            'available':  avail,
            'requested':  int(item.get('quantity', 0)),
            'ok':         avail >= int(item.get('quantity', 0)),
        })
    return jsonify({'success': True, 'data': result})


# ── CREATE ────────────────────────────────────────────────────────────────────

@bp.route('', methods=['POST'])
@token_required
@handle_exceptions
def create_so(current_user):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403
    data         = request.json or {}
    warehouse_id = data.get('warehouse_id', '').strip()
    site_ids     = _manager_site_ids(current_user)

    if not warehouse_id or not Site.query.get(warehouse_id):
        return jsonify({'success': False, 'message': 'Valid warehouse is required'}), 400
    if site_ids is not None and warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied to this warehouse'}), 403

    items_data = data.get('items', [])
    if not items_data:
        return jsonify({'success': False, 'message': 'At least one product is required'}), 400

    # Validate stock before saving
    for item in items_data:
        qty = int(item.get('quantity', 0))
        if qty <= 0:
            return jsonify({'success': False, 'message': f"Invalid quantity for {item.get('product_id')}"}), 400
        stock = StockLevel.query.filter_by(product_id=item['product_id'], site_id=warehouse_id).first()
        avail = stock.current_quantity if stock else 0
        if avail < qty:
            prod = Product.query.get(item['product_id'])
            name = prod.product_name if prod else item['product_id']
            return jsonify({'success': False,
                'message': f"Insufficient stock for '{name}'. Available: {avail}, Requested: {qty}"}), 400

    order_date = datetime.now(timezone.utc).date()
    try:
        if data.get('order_date'):
            order_date = datetime.strptime(data['order_date'], '%Y-%m-%d').date()
    except ValueError:
        pass

    total = sum(float(i.get('unit_price', 0)) * int(i.get('quantity', 0)) for i in items_data)

    # ── Apply promotion discounts ────────────────────────────────────────────
    # For each item, check if an active promotion exists for (product, warehouse)
    # on today's date. Accumulate total discount amount, then subtract from total.
    today = datetime.now(timezone.utc).date()
    total_discount = 0.0
    for item in items_data:
        qty   = int(item.get('quantity', 0))
        price = float(item.get('unit_price', 0))
        line  = qty * price
        promo = Promotion.query.filter(
            Promotion.product_id == item['product_id'],
            Promotion.site_id    == warehouse_id,
            Promotion.start_date <= today,
            Promotion.end_date   >= today,
        ).first()
        if promo and promo.discount_amount:
            d_amount = float(promo.discount_amount)
            if promo.discount_type == 'Percentage':
                total_discount += round(line * d_amount / 100, 2)
            elif promo.discount_type == 'Flat':
                total_discount += round(min(d_amount * qty, line), 2)

    discounted_total = round(max(total - total_discount, 0), 2)
    # ────────────────────────────────────────────────────────────────────────
    for attempt in range(5):
        so_number = _generate_so_number()
        try:
            new_so = SalesOrder(
                so_number        = so_number,
                warehouse_id     = warehouse_id,
                customer_id      = data.get('customer_id') or None,
                order_date       = order_date,
                status           = 'Draft',
                total_amount     = discounted_total,
                discount         = round(total_discount, 2),
                created_by       = current_user.id,
                shipping_address = data.get('shipping_address', ''),
                transport        = data.get('transport') or None,
                notes            = data.get('notes', ''),
                email_sent       = False,
            )
            db.session.add(new_so)
            db.session.flush()

            for item in items_data:
                qty   = int(item.get('quantity', 0))
                price = float(item.get('unit_price', 0))
                db.session.add(SalesOrderItem(
                    so_id      = new_so.id,
                    product_id = item['product_id'],
                    quantity   = qty,
                    unit_price = price,
                    line_total = round(qty * price, 2),
                ))

            db.session.commit()
            break  # success — exit retry loop

        except IntegrityError as e:
            db.session.rollback()
            if 'so_number' in str(e.orig) and attempt < 4:
                continue  # duplicate SO number — regenerate and retry
            raise  # unexpected error or exhausted retries

    log_audit(current_user, 'CREATE', 'SalesOrder', new_so.id, {'so_number': so_number})
    return jsonify({
        'success':    True,
        'message':    f'Sales Order {so_number} created successfully',
        'so_id':      new_so.id,
        'so_number':  so_number,
    }), 201


# ── INVOICE DOWNLOAD ──────────────────────────────────────────────────────────

@bp.route('/<int:id>/invoice', methods=['GET'])
@token_required
@handle_exceptions
def download_so_invoice(current_user, id):
    """Stream a freshly generated PDF invoice — no disk I/O."""
    so = SalesOrder.query.get_or_404(id)
    buffer = generate_sales_invoice(so)
    return send_file(buffer,
                     mimetype='application/pdf',
                     as_attachment=True,
                     download_name=f"{so.so_number}.pdf")


# ── CONFIRM ───────────────────────────────────────────────────────────────────

@bp.route('/<int:id>/confirm', methods=['POST'])
@token_required
@handle_exceptions
def confirm_so(current_user, id):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403
    so = SalesOrder.query.get(id)
    if not so:
        return jsonify({'success': False, 'message': 'Sales Order not found'}), 404
    if so.status != 'Draft':
        return jsonify({'success': False, 'message': f'Cannot confirm — status is {so.status}'}), 400
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and so.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    # Re-validate stock at confirm time
    for item in so.items:
        stock = StockLevel.query.filter_by(product_id=item.product_id, site_id=so.warehouse_id).first()
        avail = stock.current_quantity if stock else 0
        if avail < item.quantity:
            prod = item.product
            return jsonify({'success': False,
                'message': f"Insufficient stock for '{prod.product_name if prod else item.product_id}'. "
                           f"Available: {avail}, Required: {item.quantity}"}), 400

    so.status       = 'Confirmed'
    so.confirmed_by = current_user.id
    db.session.commit()
    log_audit(current_user, 'CONFIRM', 'SalesOrder', id, {'so_number': so.so_number})

    items_data = [{'product_name': (i.product.product_name if i.product else i.product_id),
                   'quantity': i.quantity, 'unit_price': float(i.unit_price or 0),
                   'line_total': float(i.line_total or 0)} for i in so.items]

    cust      = so.customer
    email     = cust.email if cust and cust.email else None
    sent      = False
    if email and _is_valid_email(email):
        html = _build_email_html(so, items_data, 'Confirmed')
        _send_async(current_app._get_current_object(),
                    f'[InventoHub] Sales Order {so.so_number} Confirmed', [email], html)
        so.email_sent = True
        db.session.commit()
        sent = True

    return jsonify({'success': True, 'message': f'Order {so.so_number} confirmed.', 'email_sent': sent})


# ── SEND EMAIL ────────────────────────────────────────────────────────────────

@bp.route('/<int:id>/send-email', methods=['POST'])
@token_required
@handle_exceptions
def send_so_email(current_user, id):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403
    so = SalesOrder.query.get(id)
    if not so:
        return jsonify({'success': False, 'message': 'SO not found'}), 404
    if so.status == 'Draft':
        return jsonify({'success': False, 'message': 'Confirm the order before sending email'}), 400
    if so.status == 'Cancelled':
        return jsonify({'success': False, 'message': 'Cannot send email for a cancelled order'}), 400

    data           = request.json or {}
    override_email = data.get('recipient_email', '').strip()
    cust           = so.customer
    recipient      = override_email or (cust.email if cust and cust.email else '')

    if not _is_valid_email(recipient):
        return jsonify({'success': False, 'message': 'No valid email. Please provide one.',
                        'needs_email': True}), 400

    items_data = [{'product_name': (i.product.product_name if i.product else i.product_id),
                   'quantity': i.quantity, 'unit_price': float(i.unit_price or 0),
                   'line_total': float(i.line_total or 0)} for i in so.items]

    html = _build_email_html(so, items_data, so.status)
    _send_async(current_app._get_current_object(),
                f'[InventoHub] SO {so.so_number} — {so.status}', [recipient], html)
    so.email_sent = True
    db.session.commit()
    log_audit(current_user, 'EMAIL', 'SalesOrder', id, {'recipient': recipient})
    return jsonify({'success': True, 'message': f'Email queued for {recipient}'})


# ── SHIP ──────────────────────────────────────────────────────────────────────

@bp.route('/<int:id>/ship', methods=['POST'])
@token_required
@handle_exceptions
def ship_so(current_user, id):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403
    so = SalesOrder.query.get(id)
    if not so:
        return jsonify({'success': False, 'message': 'SO not found'}), 404
    if so.status != 'Confirmed':
        return jsonify({'success': False,
            'message': f'Only Confirmed orders can be dispatched. Status: {so.status}'}), 400
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and so.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    data      = request.json or {}
    ship_items = data.get('items', [])
    ship_map  = {i['product_id']: int(i.get('shipped_quantity', i.get('quantity', 0))) for i in ship_items} if ship_items else {}

    items_data = []
    for item in so.items:
        shipped_qty = ship_map.get(item.product_id, item.quantity)
        if shipped_qty <= 0:
            continue

        stock = StockLevel.query.filter_by(
            product_id=item.product_id, site_id=so.warehouse_id).with_for_update().first()
        avail = stock.current_quantity if stock else 0
        if avail < shipped_qty:
            prod = item.product
            return jsonify({'success': False,
                'message': f"Insufficient stock for '{prod.product_name if prod else item.product_id}'. "
                           f"Available: {avail}, Requested: {shipped_qty}"}), 400

        stock.current_quantity -= shipped_qty
        db.session.add(StockMovement(
            product_id    = item.product_id,
            site_id       = so.warehouse_id,
            quantity      = -shipped_qty,
            movement_type = 'SalesOut',
            reference_id  = so.so_number,
            notes         = f'Dispatched for {so.so_number}',
            created_by    = current_user.username,
        ))
        item.shipped_quantity = shipped_qty

        # ── Update Sale record so inventory "Sales Activity" stays in sync ──
        sale_rec = Sale.query.filter_by(
            product_id=item.product_id, site_id=so.warehouse_id
        ).first()
        # Revenue for shipped portion = unit_price × shipped_qty
        item_revenue = float(item.unit_price or 0) * shipped_qty
        if sale_rec:
            sale_rec.units_sold = (sale_rec.units_sold or 0) + shipped_qty
            sale_rec.revenue    = float(sale_rec.revenue or 0) + item_revenue
        else:
            db.session.add(Sale(
                site_id=so.warehouse_id,
                product_id=item.product_id,
                units_sold=shipped_qty,
                revenue=item_revenue,
                returns=0,
                date=so.order_date or datetime.now(timezone.utc).date(),
                customer_id=so.customer_id,
            ))

        prod = item.product
        items_data.append({'product_name': prod.product_name if prod else item.product_id,
                           'quantity': shipped_qty, 'unit_price': float(item.unit_price or 0),
                           'line_total': float(item.line_total or 0)})

    so.status = 'Shipped'
    if data.get('transport'):
        so.transport = data['transport']
    db.session.commit()
    log_audit(current_user, 'SHIP', 'SalesOrder', id, {'so_number': so.so_number})

    # ── Write dispatched items to Logistics_Data.csv ──────────────────────
    csv_path     = os.path.join(os.path.dirname(__file__), '../../data/Logistics_Data.csv')
    csv_path     = os.path.abspath(csv_path)
    shipment_id  = data.get('shipment_id') or f'SHP-{so.so_number}'
    # Always embed the SO number so logistics enrichment can match it reliably
    if so.so_number not in shipment_id:
        shipment_id = f'{shipment_id}-{so.so_number}'
    transport    = data.get('transport', so.transport or 'Truck')
    from datetime import date as _date
    ship_date    = so.order_date.strftime('%Y-%m-%d') if so.order_date else _date.today().strftime('%Y-%m-%d')
    try:
        with open(csv_path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            for item in so.items:
                if (item.shipped_quantity or 0) > 0:
                    writer.writerow([
                        shipment_id,
                        so.warehouse_id,
                        item.product_id,
                        ship_date,
                        item.shipped_quantity,
                        'In Transit',
                        transport,
                    ])
    except Exception as csv_err:
        current_app.logger.error(f'Logistics CSV write error: {csv_err}')

    # ── Also write dispatched items to the Logistics DB table ─────────────
    try:
        from datetime import date as _date2
        ship_date_obj = so.order_date if so.order_date else _date2.today()
        for item in so.items:
            if (item.shipped_quantity or 0) > 0:
                unique_shp_id = f'{shipment_id}-{item.product_id}'
                if not Logistics.query.filter_by(shipment_id=unique_shp_id).first():
                    db.session.add(Logistics(
                        shipment_id=unique_shp_id,
                        site_id=so.warehouse_id,
                        product_id=item.product_id,
                        shipment_date=ship_date_obj,
                        quantity=item.shipped_quantity,
                        delivery_status='In Transit',
                        transportation_type=transport,
                    ))
        db.session.commit()
    except Exception as db_err:
        db.session.rollback()
        current_app.logger.error(f'Logistics DB write error: {db_err}')

    cust  = so.customer
    email = cust.email if cust and cust.email else None
    sent  = False
    if email and _is_valid_email(email):
        html = _build_email_html(so, items_data, 'Shipped')
        _send_async(current_app._get_current_object(),
                    f'[InventoHub] Your Order {so.so_number} Has Been Dispatched', [email], html)
        sent = True

    return jsonify({'success': True, 'message': f'Order {so.so_number} dispatched. Stock updated.',
                    'email_sent': sent, 'so_number': so.so_number})


# ── CANCEL ────────────────────────────────────────────────────────────────────

@bp.route('/<int:id>/cancel', methods=['POST'])
@token_required
@handle_exceptions
def cancel_so(current_user, id):
    if current_user.role.role_name == 'Analyst':
        return jsonify({'success': False, 'message': 'Analysts have view-only access'}), 403
    so = SalesOrder.query.get(id)
    if not so:
        return jsonify({'success': False, 'message': 'SO not found'}), 404
    if so.status in ('Shipped', 'Cancelled'):
        return jsonify({'success': False, 'message': f'Cannot cancel a {so.status} order'}), 400
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None and so.warehouse_id not in site_ids:
        return jsonify({'success': False, 'message': 'Access denied'}), 403
    so.status = 'Cancelled'
    db.session.commit()
    log_audit(current_user, 'CANCEL', 'SalesOrder', id, {'so_number': so.so_number})
    return jsonify({'success': True, 'message': f'SO {so.so_number} cancelled'})
