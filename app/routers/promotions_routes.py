from flask import Blueprint, request, jsonify
from app.models import Promotion, Product, Site, db
from app.auth import token_required, role_required, log_audit
from app.dependencies import handle_exceptions
from sqlalchemy import func, or_
from datetime import date, datetime

bp = Blueprint('promotions', __name__, url_prefix='/api/promotions')


def _generate_promotion_id():
    """Auto-generate next promotion ID in format PROMO10000, PROMO10001, ..."""
    last = db.session.query(Promotion.promotion_id)\
        .filter(Promotion.promotion_id.like('PROMO%'))\
        .order_by(Promotion.promotion_id.desc()).first()
    if last:
        try:
            num = int(last[0][5:]) + 1
        except Exception:
            num = 10000
    else:
        num = 10000
    return f'PROMO{num}'


def promotion_to_dict(p):
    product = Product.query.get(p.product_id) if p.product_id else None
    site = Site.query.get(p.site_id) if p.site_id else None
    today = date.today()

    if p.start_date and p.end_date:
        if today < p.start_date:
            status = 'Scheduled'
        elif p.start_date <= today <= p.end_date:
            status = 'Active'
        else:
            status = 'Expired'
    else:
        status = 'Unknown'

    return {
        'id': p.id,
        'promotion_id': p.promotion_id,
        'product_id': p.product_id,
        'product_name': product.product_name if product else 'N/A',
        'product_category': product.category if product else 'N/A',
        'site_id': p.site_id,
        'site_name': site.site_name if site else 'N/A',
        'start_date': p.start_date.isoformat() if p.start_date else None,
        'end_date': p.end_date.isoformat() if p.end_date else None,
        'discount_type': p.discount_type,
        'discount_amount': float(p.discount_amount) if p.discount_amount else 0,
        'status': status,
    }


# ─── GET ALL PROMOTIONS ──────────────────────────────────────────────────────
@bp.route('', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_promotions(current_user):
    page        = request.args.get('page', 1, type=int)
    per_page    = request.args.get('per_page', 15, type=int)
    search      = request.args.get('search', '', type=str).strip()
    status_f    = request.args.get('status', '', type=str).strip()
    dtype_f     = request.args.get('discount_type', '', type=str).strip()
    sort_by     = request.args.get('sort_by', 'id', type=str)
    sort_order  = request.args.get('sort_order', 'desc', type=str)

    query = Promotion.query

    # Manager sees only promotions for their state's sites
    if current_user.role.role_name == 'Manager' and current_user.state_id and current_user.state_id != 'ALL':
        try:
            state_id = int(current_user.state_id)
            from app.models import Site
            state_site_ids = [s.site_id for s in Site.query.filter_by(state_id=state_id).all()]
            query = query.filter(Promotion.site_id.in_(state_site_ids))
        except Exception:
            pass

    # Search
    if search:
        query = query.filter(or_(
            Promotion.promotion_id.ilike(f'%{search}%'),
            Promotion.product_id.ilike(f'%{search}%'),
            Promotion.site_id.ilike(f'%{search}%'),
        ))

    # Discount type filter
    if dtype_f:
        query = query.filter(Promotion.discount_type == dtype_f)

    # Sorting
    sort_col = getattr(Promotion, sort_by, Promotion.id)
    if sort_order == 'asc':
        query = query.order_by(sort_col.asc())
    else:
        query = query.order_by(sort_col.desc())

    all_promos = query.all()

    # Status filter after computing derived status
    today = date.today()
    def get_status(p):
        if p.start_date and p.end_date:
            if today < p.start_date:
                return 'Scheduled'
            elif p.start_date <= today <= p.end_date:
                return 'Active'
            else:
                return 'Expired'
        return 'Unknown'

    if status_f:
        all_promos = [p for p in all_promos if get_status(p) == status_f]

    total = len(all_promos)
    start = (page - 1) * per_page
    paginated = all_promos[start:start + per_page]

    # Stats summary
    active_count    = sum(1 for p in all_promos if get_status(p) == 'Active')
    scheduled_count = sum(1 for p in all_promos if get_status(p) == 'Scheduled')
    expired_count   = sum(1 for p in all_promos if get_status(p) == 'Expired')
    pct_promos      = [p for p in all_promos if p.discount_type == 'Percentage']
    flat_promos     = [p for p in all_promos if p.discount_type == 'Flat']

    return jsonify({
        'success': True,
        'promotions': [promotion_to_dict(p) for p in paginated],
        'pagination': {
            'page': page,
            'per_page': per_page,
            'total': total,
            'pages': (total + per_page - 1) // per_page
        },
        'stats': {
            'total': Promotion.query.count(),
            'active': active_count,
            'scheduled': scheduled_count,
            'expired': expired_count,
            'percentage_count': len(pct_promos),
            'flat_count': len(flat_promos),
        }
    })


# ─── GET SINGLE PROMOTION ────────────────────────────────────────────────────
@bp.route('/<int:promo_id>', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_promotion(current_user, promo_id):
    p = Promotion.query.get_or_404(promo_id)
    return jsonify({'success': True, 'promotion': promotion_to_dict(p)})


# ─── CREATE PROMOTION ────────────────────────────────────────────────────────
@bp.route('', methods=['POST'])
@token_required
@role_required('Admin')
@handle_exceptions
def create_promotion(current_user):
    data = request.get_json()

    errors = []
    if not data.get('product_id'):
        errors.append('Product ID is required.')
    if not data.get('site_id'):
        errors.append('Site ID is required.')
    if not data.get('start_date'):
        errors.append('Start date is required.')
    if not data.get('end_date'):
        errors.append('End date is required.')
    if not data.get('discount_type'):
        errors.append('Discount type is required.')
    if data.get('discount_amount') is None:
        errors.append('Discount amount is required.')

    if errors:
        return jsonify({'success': False, 'errors': errors}), 400

    # Validate product & site exist
    product = Product.query.get(data['product_id'])
    if not product:
        return jsonify({'success': False, 'errors': ['Product not found.']}), 404
    site = Site.query.get(data['site_id'])
    if not site:
        return jsonify({'success': False, 'errors': ['Site not found.']}), 404

    try:
        start_date = datetime.strptime(data['start_date'], '%Y-%m-%d').date()
        end_date   = datetime.strptime(data['end_date'],   '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'success': False, 'errors': ['Invalid date format. Use YYYY-MM-DD.']}), 400

    if end_date < start_date:
        return jsonify({'success': False, 'errors': ['End date must be after start date.']}), 400

    if data['discount_type'] not in ('Percentage', 'Flat'):
        return jsonify({'success': False, 'errors': ['Discount type must be Percentage or Flat.']}), 400

    try:
        discount_amount = float(data['discount_amount'])
        if discount_amount <= 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'success': False, 'errors': ['Discount amount must be a positive number.']}), 400

    if data['discount_type'] == 'Percentage' and discount_amount > 100:
        return jsonify({'success': False, 'errors': ['Percentage discount cannot exceed 100%.']}), 400

    promo = Promotion(
        promotion_id    = _generate_promotion_id(),
        product_id      = data['product_id'],
        site_id         = data['site_id'],
        start_date      = start_date,
        end_date        = end_date,
        discount_type   = data['discount_type'],
        discount_amount = discount_amount,
    )
    db.session.add(promo)
    db.session.commit()

    log_audit(current_user, 'CREATE', 'Promotion', promo.promotion_id, {
        'product_id': promo.product_id,
        'site_id': promo.site_id,
        'discount_type': promo.discount_type,
        'discount_amount': float(promo.discount_amount),
    })

    return jsonify({'success': True, 'message': 'Promotion created successfully.', 'promotion': promotion_to_dict(promo)}), 201


# ─── UPDATE PROMOTION ────────────────────────────────────────────────────────
@bp.route('/<int:promo_id>', methods=['PUT'])
@token_required
@role_required('Admin')
@handle_exceptions
def update_promotion(current_user, promo_id):
    p    = Promotion.query.get_or_404(promo_id)
    data = request.get_json()

    if 'product_id' in data:
        if not Product.query.get(data['product_id']):
            return jsonify({'success': False, 'errors': ['Product not found.']}), 404
        p.product_id = data['product_id']

    if 'site_id' in data:
        if not Site.query.get(data['site_id']):
            return jsonify({'success': False, 'errors': ['Site not found.']}), 404
        p.site_id = data['site_id']

    if 'start_date' in data:
        try:
            p.start_date = datetime.strptime(data['start_date'], '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'success': False, 'errors': ['Invalid start_date format.']}), 400

    if 'end_date' in data:
        try:
            p.end_date = datetime.strptime(data['end_date'], '%Y-%m-%d').date()
        except ValueError:
            return jsonify({'success': False, 'errors': ['Invalid end_date format.']}), 400

    if p.end_date and p.start_date and p.end_date < p.start_date:
        return jsonify({'success': False, 'errors': ['End date must be after start date.']}), 400

    if 'discount_type' in data:
        if data['discount_type'] not in ('Percentage', 'Flat'):
            return jsonify({'success': False, 'errors': ['Discount type must be Percentage or Flat.']}), 400
        p.discount_type = data['discount_type']

    if 'discount_amount' in data:
        try:
            amount = float(data['discount_amount'])
            if amount <= 0:
                raise ValueError
            if p.discount_type == 'Percentage' and amount > 100:
                return jsonify({'success': False, 'errors': ['Percentage cannot exceed 100%.']}), 400
            p.discount_amount = amount
        except (ValueError, TypeError):
            return jsonify({'success': False, 'errors': ['Discount amount must be a positive number.']}), 400

    db.session.commit()

    log_audit(current_user, 'UPDATE', 'Promotion', p.promotion_id, {
        'product_id': p.product_id,
        'discount_type': p.discount_type,
        'discount_amount': float(p.discount_amount),
    })

    return jsonify({'success': True, 'message': 'Promotion updated successfully.', 'promotion': promotion_to_dict(p)})


# ─── DELETE PROMOTION ────────────────────────────────────────────────────────
@bp.route('/<int:promo_id>', methods=['DELETE'])
@token_required
@role_required('Admin')
@handle_exceptions
def delete_promotion(current_user, promo_id):
    p = Promotion.query.get_or_404(promo_id)
    pid = p.promotion_id

    log_audit(current_user, 'DELETE', 'Promotion', pid, {
        'product_id': p.product_id,
        'site_id': p.site_id,
    })

    db.session.delete(p)
    db.session.commit()
    return jsonify({'success': True, 'message': f'Promotion {pid} deleted.'})


# ─── BULK DELETE ─────────────────────────────────────────────────────────────
@bp.route('/bulk-delete', methods=['POST'])
@token_required
@role_required('Admin')
@handle_exceptions
def bulk_delete(current_user):
    data = request.get_json()
    ids  = data.get('ids', [])
    if not ids:
        return jsonify({'success': False, 'errors': ['No IDs provided.']}), 400

    deleted = 0
    for pid in ids:
        p = Promotion.query.get(pid)
        if p:
            log_audit(current_user, 'DELETE', 'Promotion', p.promotion_id, {'bulk': True})
            db.session.delete(p)
            deleted += 1

    db.session.commit()
    return jsonify({'success': True, 'message': f'{deleted} promotion(s) deleted.'})


# ─── STATS SUMMARY ───────────────────────────────────────────────────────────
@bp.route('/stats/summary', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def stats_summary(current_user):
    today = date.today()

    # Base query — Manager sees only their state's sites
    base_q = Promotion.query
    if current_user.role.role_name == 'Manager' and current_user.state_id and current_user.state_id != 'ALL':
        try:
            state_id = int(current_user.state_id)
            state_site_ids = [s.site_id for s in Site.query.filter_by(state_id=state_id).all()]
            base_q = base_q.filter(Promotion.site_id.in_(state_site_ids))
        except Exception:
            pass

    all_p  = base_q.all()

    active    = [p for p in all_p if p.start_date and p.end_date and p.start_date <= today <= p.end_date]
    scheduled = [p for p in all_p if p.start_date and today < p.start_date]
    expired   = [p for p in all_p if p.end_date and today > p.end_date]

    # Averages scoped to same filtered set
    pct_amounts  = [float(p.discount_amount) for p in all_p if p.discount_type == 'Percentage']
    flat_amounts = [float(p.discount_amount) for p in all_p if p.discount_type == 'Flat']
    avg_pct  = round(sum(pct_amounts)  / len(pct_amounts),  2) if pct_amounts  else 0
    avg_flat = round(sum(flat_amounts) / len(flat_amounts), 2) if flat_amounts else 0

    pct_count  = sum(1 for p in all_p if p.discount_type == 'Percentage')
    flat_count = sum(1 for p in all_p if p.discount_type == 'Flat')

    # Top 5 products by promotion count (within filtered set)
    from collections import Counter
    prod_counts = Counter(p.product_id for p in all_p)
    top_products_data = []
    for pid, cnt in prod_counts.most_common(5):
        prod = Product.query.get(pid)
        top_products_data.append({
            'product_id': pid,
            'product_name': prod.product_name if prod else pid,
            'count': cnt
        })

    return jsonify({
        'success': True,
        'stats': {
            'total': len(all_p),
            'active': len(active),
            'scheduled': len(scheduled),
            'expired': len(expired),
            'avg_percentage_discount': avg_pct,
            'avg_flat_discount': avg_flat,
            'percentage_count': pct_count,
            'flat_count': flat_count,
            'top_products': top_products_data,
        }
    })


# ─── SEARCH PRODUCTS (for autocomplete) ──────────────────────────────────────
@bp.route('/search/products', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def search_products(current_user):
    q = request.args.get('q', '', type=str).strip()
    if len(q) < 1:
        products = Product.query.limit(200).all()
    else:
        products = Product.query.filter(or_(
            Product.product_id.ilike(f'%{q}%'),
            Product.product_name.ilike(f'%{q}%'),
        )).limit(200).all()
    return jsonify({'success': True, 'products': [
        {'product_id': p.product_id, 'product_name': p.product_name, 'category': p.category, 'unit_price': float(p.unit_price)}
        for p in products
    ]})


# ─── SEARCH SITES (for autocomplete) ─────────────────────────────────────────
@bp.route('/search/sites', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def search_sites(current_user):
    q = request.args.get('q', '', type=str).strip()
    if len(q) < 1:
        sites = Site.query.limit(200).all()
    else:
        sites = Site.query.filter(or_(
            Site.site_id.ilike(f'%{q}%'),
            Site.site_name.ilike(f'%{q}%'),
        )).limit(200).all()
    return jsonify({'success': True, 'sites': [
        {'site_id': s.site_id, 'site_name': s.site_name, 'city': s.city, 'region': s.region}
        for s in sites
    ]})
