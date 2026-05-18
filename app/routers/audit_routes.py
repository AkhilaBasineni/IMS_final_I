from flask import Blueprint, jsonify, request
from app.models import AuditLog
from app.auth import token_required, role_required
from app.dependencies import handle_exceptions

bp = Blueprint('audit', __name__, url_prefix='/api/audit-logs')

@bp.route('', methods=['GET'])
@token_required
@role_required('Admin')
@handle_exceptions
def get_audit_logs(current_user):
    page     = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))

    pagination = (AuditLog.query
                  .filter(AuditLog.created_at.isnot(None))
                  .order_by(AuditLog.created_at.desc())
                  .paginate(page=page, per_page=per_page, error_out=False))

    return jsonify({
        'success': True,
        'data': {
            'items': [{
                'username':    l.username,
                'action':      l.action,
                'entity_type': l.entity_type,
                'entity_id':   l.entity_id,
                'created_at':  l.created_at.strftime('%Y-%m-%d %H:%M:%S')
            } for l in pagination.items],
            'current_page': pagination.page,
            'pages':        pagination.pages,
            'total':        pagination.total,
        }
    })
