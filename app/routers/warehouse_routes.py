from flask import Blueprint, request, jsonify
from app.models import Site, State, Manager, db
from app.auth import token_required, role_required, log_audit
from app.dependencies import handle_exceptions
from sqlalchemy import or_

bp = Blueprint('warehouses', __name__, url_prefix='/api/warehouses')


def _generate_site_id():
    """Auto-generate next site ID in format SITE1000, SITE1001, ..."""
    last = db.session.query(Site.site_id)\
        .filter(Site.site_id.like('SITE%'))\
        .order_by(Site.site_id.desc()).first()
    if last:
        try:
            num = int(last[0][4:]) + 1
        except Exception:
            num = 1000
    else:
        num = 1000
    return f'SITE{num}'


def _manager_state_id(current_user):
    """Returns int state_id for Manager, or None if unrestricted."""
    if current_user.role.role_name in ('Manager', 'Analyst') and current_user.state_id and current_user.state_id != 'ALL':
        try:
            return int(current_user.state_id)
        except (ValueError, TypeError):
            return None
    return None


def _apply_manager_filter(query, current_user):
    """Restrict sites to manager's state."""
    state_id = _manager_state_id(current_user)
    if state_id is not None:
        query = query.filter(Site.state_id == state_id)
    return query


@bp.route('', methods=['GET'])
@token_required
@handle_exceptions
def get_warehouses(current_user):
    page   = request.args.get('page', 1, type=int)
    search = request.args.get('search', '').strip()

    query = Site.query.outerjoin(State, Site.state_id == State.state_id)
    query = _apply_manager_filter(query, current_user)

    if search:
        query = query.filter(or_(
            Site.site_id.ilike(f'%{search}%'),
            Site.site_name.ilike(f'%{search}%'),
            Site.city.ilike(f'%{search}%'),
            State.state_name.ilike(f'%{search}%')
        ))

    pagination = query.order_by(Site.created_at.desc()).paginate(
        page=page, per_page=10, error_out=False
    )

    results = [{
        'site_id':      s.site_id,
        'site_name':    s.site_name,
        'site_format':  s.site_format,
        'region':       s.region,
        'city':         s.city,
        'state':        s.state.state_name if s.state else '',
        'store_size':   s.store_size,
        'open_date':    s.open_date.strftime('%Y-%m-%d') if s.open_date else '',
        'manager_name': s.manager.manager_name if s.manager else 'Unassigned',
        'status':       s.status
    } for s in pagination.items]

    return jsonify({
        'success': True,
        'data': {
            'items':        results,
            'total':        pagination.total,
            'pages':        pagination.pages,
            'current_page': page
        }
    })


@bp.route('/<site_id>', methods=['GET'])
@token_required
@handle_exceptions
def get_warehouse(current_user, site_id):
    s = Site.query.get(site_id)
    if not s:
        return jsonify({'success': False, 'message': 'Warehouse not found'}), 404

    state_id = _manager_state_id(current_user)
    if state_id is not None and s.state_id != state_id:
        return jsonify({'success': False, 'message': 'Access denied'}), 403

    return jsonify({
        'success': True,
        'data': {
            'site_id':      s.site_id,
            'site_name':    s.site_name,
            'site_format':  s.site_format  or '',
            'region':       s.region       or '',
            'city':         s.city         or '',
            'state':        s.state.state_name if s.state else '',
            'store_size':   s.store_size,
            'open_date':    s.open_date.strftime('%Y-%m-%d') if s.open_date else '',
            'manager_name': s.manager.manager_name if s.manager else '',
            'status':       s.status       or 'Active'
        }
    })


@bp.route('', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def create_warehouse(current_user):
    data = request.json

    # Auto-generate site_id (ignore any site_id sent in payload)
    generated_id = _generate_site_id()

    # Ensure uniqueness (safety check in case of race)
    while Site.query.get(generated_id):
        try:
            num = int(generated_id[4:]) + 1
        except Exception:
            num = 1000
        generated_id = f'SITE{num}'

    if not data.get('site_name', '').strip():
        return jsonify({'success': False, 'message': 'site_name is required'}), 400

    # Manager: force state_id to their own state — ignore any state sent in payload
    manager_state_id = _manager_state_id(current_user)

    if manager_state_id is not None:
        # Manager: always assign to their own state
        state_obj = State.query.get(manager_state_id)
        if not state_obj:
            return jsonify({'success': False, 'message': 'Your assigned state not found'}), 400
    else:
        # Admin: use state from payload
        state_obj = None
        if data.get('state'):
            state_obj = State.query.filter_by(state_name=data['state']).first()
            if not state_obj:
                state_obj = State(state_name=data['state'])
                db.session.add(state_obj)
                db.session.flush()

    manager_obj = None
    if data.get('manager_name'):
        manager_obj = Manager.query.filter_by(manager_name=data['manager_name']).first()
        if not manager_obj:
            manager_obj = Manager(manager_name=data['manager_name'])
            db.session.add(manager_obj)
            db.session.flush()

    site = Site(
        site_id     = generated_id,
        site_name   = data['site_name'],
        site_format = data.get('site_format'),
        region      = data.get('region'),
        city        = data.get('city'),
        state_id    = state_obj.state_id if state_obj else None,
        manager_id  = manager_obj.manager_id if manager_obj else None,
        store_size  = data.get('store_size'),
        open_date   = data.get('open_date') or None,
        status      = data.get('status', 'Active')
    )
    db.session.add(site)
    db.session.commit()
    log_audit(current_user, 'CREATE', 'Warehouse', site.site_id)
    return jsonify({'success': True, 'message': 'Warehouse created successfully', 'site_id': site.site_id}), 201


@bp.route('/<site_id>', methods=['PUT'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def update_warehouse(current_user, site_id):
    s = Site.query.get(site_id)
    if not s:
        return jsonify({'success': False, 'message': 'Warehouse not found'}), 404

    # Manager: only update sites in their state
    state_id = _manager_state_id(current_user)
    if state_id is not None and s.state_id != state_id:
        return jsonify({'success': False, 'message': 'Access denied — site not in your state'}), 403

    data = request.json

    # Admin can change state; Manager cannot move a site out of their state
    if 'state' in data and state_id is None:
        if data['state']:
            state_obj = State.query.filter_by(state_name=data['state']).first()
            if not state_obj:
                state_obj = State(state_name=data['state'])
                db.session.add(state_obj)
                db.session.flush()
            s.state_id = state_obj.state_id
        else:
            s.state_id = None

    s.site_name   = data.get('site_name',   s.site_name)
    s.site_format = data.get('site_format', s.site_format)
    s.region      = data.get('region',      s.region)
    s.city        = data.get('city',        s.city)
    s.store_size  = data.get('store_size',  s.store_size)
    s.open_date   = data.get('open_date')  or s.open_date
    s.status      = data.get('status',      s.status)

    db.session.commit()
    log_audit(current_user, 'UPDATE', 'Warehouse', site_id)
    return jsonify({'success': True, 'message': 'Warehouse updated successfully'})


@bp.route('/<site_id>', methods=['DELETE'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def delete_warehouse(current_user, site_id):
    s = Site.query.get(site_id)
    if not s:
        return jsonify({'success': False, 'message': 'Warehouse not found'}), 404

    # Manager: only delete sites in their state
    state_id = _manager_state_id(current_user)
    if state_id is not None and s.state_id != state_id:
        return jsonify({'success': False, 'message': 'Access denied — site not in your state'}), 403

    db.session.delete(s)
    db.session.commit()
    log_audit(current_user, 'DELETE', 'Warehouse', site_id)
    return jsonify({'success': True, 'message': 'Warehouse deleted successfully'})


@bp.route('/cities-by-state', methods=['GET'])
@token_required
@handle_exceptions
def cities_by_state(current_user):
    """Return distinct cities for a given state name."""
    state_name = request.args.get('state', '').strip()
    if not state_name:
        return jsonify({'success': True, 'data': []})
    state = State.query.filter_by(state_name=state_name).first()
    if not state:
        return jsonify({'success': True, 'data': []})
    cities = db.session.query(Site.city).filter(
        Site.state_id == state.state_id,
        Site.city.isnot(None),
        Site.city != ''
    ).distinct().order_by(Site.city).all()
    return jsonify({'success': True, 'data': [c[0] for c in cities if c[0]]})
