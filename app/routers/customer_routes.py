from flask import Blueprint, request, jsonify
from app.models import Customer, Sale, Site, SalesOrder, db
from app.auth import token_required, role_required, log_audit
from app.dependencies import handle_exceptions
from sqlalchemy import or_, func
import uuid, re

bp = Blueprint('customers', __name__, url_prefix='/api/customers')


def _state_customer_ids(current_user):
    """
    Admin  → None  (no restriction, load all customers).
    Manager/Analyst → list of customer_ids who have purchased at a site in their assigned state.
    """
    role = current_user.role.role_name
    if role not in ('Manager', 'Analyst'):
        return None
    state_id = current_user.state_id
    if not state_id or state_id == 'ALL':
        return None

    try:
        site_ids = [
            s.site_id
            for s in Site.query.filter_by(state_id=int(state_id)).all()
        ]
    except (ValueError, TypeError):
        return []

    if not site_ids:
        return []

    rows = (
        db.session.query(Sale.customer_id)
        .filter(Sale.site_id.in_(site_ids), Sale.customer_id.isnot(None))
        .distinct()
        .all()
    )
    return [r.customer_id for r in rows]


def _format_customer(c, include_stats=False):
    data = {
        'customer_id':        c.customer_id,
        'name':               getattr(c, 'name', None) or '',
        'email':              getattr(c, 'email', None) or '',
        'age':                c.age,
        'gender':             c.gender or '—',
        'income_bracket':     c.income_bracket or '—',
        'purchase_frequency': c.purchase_frequency,
        'average_spend':      float(c.average_spend or 0),
    }
    if include_stats:
        stats = db.session.query(
            func.count(Sale.id).label('total_orders'),
            func.coalesce(func.sum(Sale.units_sold), 0).label('total_units'),
            func.coalesce(func.sum(Sale.revenue), 0).label('total_revenue'),
            func.coalesce(func.sum(Sale.returns), 0).label('total_returns'),
            func.coalesce(func.sum(Sale.discounts), 0).label('total_discounts'),
        ).filter(Sale.customer_id == c.customer_id).first()
        data['stats'] = {
            'total_orders':    int(stats.total_orders or 0),
            'total_units':     int(stats.total_units or 0),
            'total_revenue':   float(stats.total_revenue or 0),
            'total_returns':   int(stats.total_returns or 0),
            'total_discounts': float(stats.total_discounts or 0),
        }
    return data


@bp.route('', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_customers(current_user):
    page    = request.args.get('page', 1, type=int)
    search  = request.args.get('search', '').strip()
    gender  = request.args.get('gender', '').strip()
    income  = request.args.get('income', '').strip()

    query = Customer.query

    # Managers only see customers from their state's sites
    allowed_ids = _state_customer_ids(current_user)
    if allowed_ids is not None:
        query = query.filter(Customer.customer_id.in_(allowed_ids))

    if search:
        search_filters = [
            Customer.customer_id.ilike(f'%{search}%'),
            Customer.gender.ilike(f'%{search}%'),
            Customer.income_bracket.ilike(f'%{search}%'),
        ]
        # name/email columns may not exist if migration hasn't been run
        try:
            search_filters += [
                Customer.name.ilike(f'%{search}%'),
                Customer.email.ilike(f'%{search}%'),
            ]
        except Exception:
            pass
        query = query.filter(or_(*search_filters))
    if gender:
        query = query.filter(Customer.gender == gender)
    if income:
        query = query.filter(Customer.income_bracket == income)

    pagination = query.order_by(Customer.customer_id).paginate(
        page=page, per_page=15, error_out=False
    )

    return jsonify({'success': True, 'data': {
        'items':        [_format_customer(c) for c in pagination.items],
        'total':        pagination.total,
        'pages':        pagination.pages,
        'current_page': page,
    }})


@bp.route('/<string:customer_id>', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_customer(current_user, customer_id):
    c = Customer.query.get(customer_id)
    if not c:
        return jsonify({'success': False, 'message': 'Customer not found'}), 404

    # Managers cannot view customers outside their state
    allowed_ids = _state_customer_ids(current_user)
    if allowed_ids is not None and customer_id not in allowed_ids:
        return jsonify({'success': False, 'message': 'Forbidden: customer not in your state'}), 403

    # Always keep stored stats in sync with actual SalesOrder data
    freq, avg_spend = _sync_customer_stats(customer_id)
    c.purchase_frequency = freq
    c.average_spend      = avg_spend
    db.session.commit()

    return jsonify({'success': True, 'data': _format_customer(c, include_stats=True)})


@bp.route('/summary', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def customer_summary(current_user):
    # Base customer set — managers scoped to their state
    allowed_ids = _state_customer_ids(current_user)

    base_q = Customer.query
    sale_q  = db.session.query(Sale.customer_id)
    if allowed_ids is not None:
        base_q = base_q.filter(Customer.customer_id.in_(allowed_ids))
        sale_q = sale_q.filter(Sale.customer_id.in_(allowed_ids))

    total    = base_q.count()
    genders  = base_q.with_entities(Customer.gender, func.count(Customer.customer_id)).group_by(Customer.gender).all()
    incomes  = base_q.with_entities(Customer.income_bracket, func.count(Customer.customer_id)).group_by(Customer.income_bracket).all()
    avg_freq = base_q.with_entities(func.avg(Customer.purchase_frequency)).scalar() or 0
    avg_sp   = base_q.with_entities(func.avg(Customer.average_spend)).scalar() or 0

    return jsonify({'success': True, 'data': {
        'total_customers':    total,
        'avg_purchase_freq':  round(float(avg_freq), 1),
        'avg_spend':          round(float(avg_sp), 2),
        'by_gender':          {g or 'Unknown': cnt for g, cnt in genders},
        'by_income':          {i or 'Unknown': cnt for i, cnt in incomes},
    }})


@bp.route('/filters', methods=['GET'])
@token_required
@role_required('Admin', 'Manager', 'Analyst')
@handle_exceptions
def get_filters(current_user):
    allowed_ids = _state_customer_ids(current_user)
    base_q = Customer.query
    if allowed_ids is not None:
        base_q = base_q.filter(Customer.customer_id.in_(allowed_ids))

    genders = [r[0] for r in base_q.with_entities(Customer.gender).distinct().filter(Customer.gender.isnot(None)).all()]
    incomes = [r[0] for r in base_q.with_entities(Customer.income_bracket).distinct().filter(Customer.income_bracket.isnot(None)).all()]
    return jsonify({'success': True, 'data': {'genders': sorted(genders), 'income_brackets': sorted(incomes)}})

def _sync_customer_stats(customer_id):
    """Recompute purchase_frequency and average_spend from SalesOrder data."""
    stats = db.session.query(
        func.count(SalesOrder.id).label('freq'),
        func.coalesce(func.avg(SalesOrder.total_amount), 0).label('avg_spend'),
    ).filter(SalesOrder.customer_id == customer_id).first()
    return int(stats.freq or 0), float(stats.avg_spend or 0)


# ── CREATE ─────────────────────────────────────────────────────────────────────
@bp.route('', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def create_customer(current_user):
    data = request.get_json() or {}

    # Auto-generate customer_id if not provided
    customer_id = (data.get('customer_id') or '').strip()
    if not customer_id:
        customer_id = 'CUST-' + str(uuid.uuid4())[:8].upper()

    if Customer.query.get(customer_id):
        return jsonify({'success': False, 'message': f'Customer ID {customer_id} already exists'}), 400

    freq, avg_spend = _sync_customer_stats(customer_id)

    c = Customer(
        customer_id        = customer_id,
        name               = (data.get('name') or '').strip() or None,
        email              = (data.get('email') or '').strip() or None,
        age                = data.get('age') or None,
        gender             = (data.get('gender') or '').strip() or None,
        income_bracket     = (data.get('income_bracket') or '').strip() or None,
        purchase_frequency = freq,
        average_spend      = avg_spend,
    )
    db.session.add(c)
    db.session.commit()
    log_audit(current_user, 'CREATE', 'Customer', customer_id)
    return jsonify({'success': True, 'data': _format_customer(c), 'message': f'Customer {customer_id} created successfully'}), 201


# ── UPDATE ─────────────────────────────────────────────────────────────────────
@bp.route('/<string:customer_id>', methods=['PUT'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def update_customer(current_user, customer_id):
    c = Customer.query.get(customer_id)
    if not c:
        return jsonify({'success': False, 'message': 'Customer not found'}), 404

    allowed_ids = _state_customer_ids(current_user)
    if allowed_ids is not None and customer_id not in allowed_ids:
        return jsonify({'success': False, 'message': 'Forbidden'}), 403

    data = request.get_json() or {}
    if 'name'           in data: c.name           = (data['name'] or '').strip() or None
    if 'email'          in data: c.email          = (data['email'] or '').strip() or None
    if 'age'            in data: c.age            = data['age'] or None
    if 'gender'         in data: c.gender         = (data['gender'] or '').strip() or None
    if 'income_bracket' in data: c.income_bracket = (data['income_bracket'] or '').strip() or None

    # Always recompute from sales orders
    freq, avg_spend = _sync_customer_stats(customer_id)
    c.purchase_frequency = freq
    c.average_spend      = avg_spend

    db.session.commit()
    log_audit(current_user, 'UPDATE', 'Customer', customer_id)
    return jsonify({'success': True, 'data': _format_customer(c), 'message': 'Customer updated successfully'})


# ── DELETE ─────────────────────────────────────────────────────────────────────
@bp.route('/<string:customer_id>', methods=['DELETE'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def delete_customer(current_user, customer_id):
    from app.models import Sale, SalesOrder
    c = Customer.query.get(customer_id)
    if not c:
        return jsonify({'success': False, 'message': 'Customer not found'}), 404
    # Nullify FK references before deleting to avoid constraint violations
    Sale.query.filter_by(customer_id=customer_id).update({'customer_id': None})
    SalesOrder.query.filter_by(customer_id=customer_id).update({'customer_id': None})
    db.session.flush()
    db.session.delete(c)
    db.session.commit()
    log_audit(current_user, 'DELETE', 'Customer', customer_id)
    return jsonify({'success': True, 'message': f'Customer {customer_id} deleted'})
