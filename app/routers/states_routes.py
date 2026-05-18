from flask import Blueprint, request, jsonify
from app.models import State, db
from app.auth import token_required, role_required
from app.dependencies import handle_exceptions

bp = Blueprint('states', __name__, url_prefix='/api/states')

@bp.route('', methods=['GET'])
@token_required
@handle_exceptions
def get_states(current_user):
    states = State.query.order_by(State.state_name).all()
    return jsonify({'success': True, 'data': [
        {'state_id': s.state_id, 'state_name': s.state_name}
        for s in states
    ]})

@bp.route('', methods=['POST'])
@token_required
@role_required('Admin', 'Manager')
@handle_exceptions
def add_state(current_user):
    data = request.json
    name = data.get('state_name', '').strip()
    if not name:
        return jsonify({'success': False, 'message': 'State name is required'}), 400
    if State.query.filter_by(state_name=name).first():
        return jsonify({'success': False, 'message': 'State already exists'}), 400
    state = State(state_name=name)
    db.session.add(state)
    db.session.commit()
    return jsonify({'success': True, 'message': 'State added',
                    'state_id': state.state_id, 'state_name': state.state_name}), 201
