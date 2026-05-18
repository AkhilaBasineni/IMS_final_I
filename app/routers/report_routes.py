from flask import Blueprint, jsonify
from app.models import StockLevel, Product, Site, db
from app.auth import token_required
from app.dependencies import handle_exceptions
from sqlalchemy import func

bp = Blueprint('reports', __name__, url_prefix='/api/reports')


def _manager_site_ids(current_user):
    """Return list of site_ids for manager's state, or None if unrestricted."""
    if current_user.role.role_name in ('Manager', 'Analyst') and current_user.state_id and current_user.state_id != 'ALL':
        try:
            sites = Site.query.filter_by(state_id=int(current_user.state_id)).all()
            return [s.site_id for s in sites]
        except (ValueError, TypeError):
            return []
    return None


@bp.route('/stock-valuation', methods=['GET'])
@token_required
@handle_exceptions
def stock_valuation(current_user):
    query = db.session.query(
        Product.category,
        func.sum(StockLevel.current_quantity * Product.unit_cost).label('total_value'),
        func.sum(StockLevel.current_quantity).label('total_quantity')
    ).join(StockLevel, Product.product_id == StockLevel.product_id)
    
    site_ids = _manager_site_ids(current_user)
    if site_ids is not None:
        query = query.filter(StockLevel.site_id.in_(site_ids))
    
    results = query.group_by(Product.category).order_by(func.sum(StockLevel.current_quantity * Product.unit_cost).desc()).all()
    total_val = float(sum(r.total_value or 0 for r in results))
    
    return jsonify({
        'success': True,
        'data': {
            'total_value': total_val,
            'by_category': [{
                'category': r.category or 'General',
                'value': float(r.total_value or 0),
                'quantity': int(r.total_quantity or 0),
                'percentage': (float(r.total_value or 0) / total_val * 100) if total_val > 0 else 0
            } for r in results]
        }
    })