from flask import Blueprint, request, jsonify
from app.models import StockLevel, Product, Site, db
from app.auth import token_required
from sqlalchemy import or_

bp = Blueprint('stock', __name__, url_prefix='/api/stock')


def _manager_site_ids(current_user):
    if current_user.role.role_name in ('Manager', 'Analyst') and current_user.state_id and current_user.state_id != 'ALL':
        try:
            sites = Site.query.filter_by(state_id=int(current_user.state_id)).all()
            return [s.site_id for s in sites]
        except (ValueError, TypeError):
            return []
    return None


@bp.route('/levels', methods=['GET'])
@token_required
def get_levels(current_user):
    search   = request.args.get('search', '').strip()
    page     = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 10, type=int)

    query    = StockLevel.query.join(Product).join(Site, StockLevel.site_id == Site.site_id)

    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(StockLevel.site_id.in_(site_ids))

    if search:
        query = query.filter(or_(
            Product.product_name.ilike(f'%{search}%'),
            Product.product_id.ilike(f'%{search}%'),
            Product.category.ilike(f'%{search}%'),
            Product.subcategory.ilike(f'%{search}%'),
            Site.site_name.ilike(f'%{search}%'),
        ))

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'success': True,
        'data': {
            'items': [{
                'product_id':    l.product_id,
                'product_name':  l.product.product_name if l.product else 'Unknown',
                'warehouse_name': l.site.site_name if l.site else 'Unknown',
                'quantity':      l.current_quantity,
                'reorder_point': l.product.reorder_point if l.product else 0
            } for l in pagination.items],
            'total':        pagination.total,
            'pages':        pagination.pages,
            'current_page': page
        }
    })


@bp.route('/levels/low-stock', methods=['GET'])
@token_required
def get_low_stock(current_user):
    """
    Returns one entry per unique product SKU (not one per warehouse row).

    BUG FIX: The old query counted StockLevel rows directly, so a product
    stocked in N warehouses could appear N times — making Low Stock > Total
    Products.  The fix aggregates total stock across all (filtered) sites
    per product first, then compares the SUM against reorder_point.
    """
    from sqlalchemy import func

    site_ids = _manager_site_ids(current_user)

    # Step 1: sum current_quantity per product (optionally scoped to sites)
    agg_q = db.session.query(
        StockLevel.product_id,
        func.sum(StockLevel.current_quantity).label('total_qty')
    )
    if site_ids is not None:
        agg_q = agg_q.filter(StockLevel.site_id.in_(site_ids))
    agg_q = agg_q.group_by(StockLevel.product_id).subquery()

    # Step 2: keep only products whose aggregated total <= reorder_point
    results = (
        db.session.query(Product, agg_q.c.total_qty)
        .join(agg_q, Product.product_id == agg_q.c.product_id)
        .filter(agg_q.c.total_qty <= Product.reorder_point)
        .all()
    )

    return jsonify({'success': True, 'data': [{
        'product_id':       p.product_id,
        'product_name':     p.product_name,
        'warehouse_name':   'All sites',
        'current_quantity': int(total_qty),
        'reorder_point':    p.reorder_point
    } for p, total_qty in results]})
